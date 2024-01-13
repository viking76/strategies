"""
Test program for verifying wavelet prediction.
Data is encoded using a wavelet transform, each coefficient is then predicted N self.lookahead into the future and the resulting signal is 
re-encoded into a data series

The function names and approach mimic those used in the Time Series Prediction strategies
"""

# Import libraries
import numpy as np
import pandas as pd
import pywt
import scipy
import matplotlib.pyplot as plt
from regex import D, S
from sklearn.feature_selection import SelectFdr
from sklearn.preprocessing import MinMaxScaler, RobustScaler
from sklearn.discriminant_analysis import StandardScaler
from pandas import DataFrame
from sympy import print_fcode

import Wavelets
import Forecasters

from sklearn.metrics import mean_squared_error

# -----------------------------------

import time


# Define a timer decorator function
def timer(func):
    # Define a wrapper function
    def wrapper(*args, **kwargs):
        # Record the start time
        start = time.time()
        # Call the original function
        result = func(*args, **kwargs)
        # Record the end time
        end = time.time()
        # Calculate the duration
        duration = end - start
        # Print the duration
        print(f"{func.__name__} took {duration} seconds to run.")
        # Return the result
        return result

    # Return the wrapper function
    return wrapper


# -----------------------------------
model_window = 64

class WaveletPredictor:
    wavelet = None
    forecaster = None
    lookahead = 6

    coeff_table = None
    coeff_table_offset = 0
    coeff_array = None
    coeff_start_col = 0
    gain_data = None
    data = None
    curr_dataframe = None

    norm_data = False
    scale_results = True
    single_col_prediction = True
    merge_indicators = False
    training_required = True
    expanding_window = False
    detrend_data = True

    wavelet_size = 64  # Windowing should match this. Longer = better but slower with edge effects. Should be even
    model_window = wavelet_size # longer = slower
    train_min_len = wavelet_size // 2 # longer = slower
    train_max_len = wavelet_size * 2 # longer = slower
    scale_len = wavelet_size // 2 # no. recent candles to use when scaling
    win_size = wavelet_size


    # --------------------------------

    def set_data(self, dataframe:DataFrame):
        self.curr_dataframe = dataframe
        self.data = np.array(dataframe["gain"])
        self.data = np.nan_to_num(self.data)
        # self.data = self.smooth(self.data, 2)
        self.build_coefficient_table(0, np.shape(self.data)[0])
        return

    # --------------------------------

    def set_wavelet_type(self, wavelet_type: Wavelets.WaveletType):
        self.wavelet = Wavelets.make_wavelet(wavelet_type)
        return

    # --------------------------------

    def set_wavelet_len(self, wavelet_len):
        self.wavelet_size = wavelet_len
        self.model_window = self.wavelet_size
        self.train_min_len = self.wavelet_size // 2 
        self.train_max_len = self.wavelet_size * 4
        self.scale_len = min(8, self.wavelet_size // 2)
        self.win_size = self.wavelet_size
        return

    # --------------------------------

    def set_forecaster_type(self, forecaster_type: Forecasters.ForecasterType):
        self.forecaster = Forecasters.make_forecaster(forecaster_type)
        return

    # --------------------------------

    def set_lookahead(self, lookahead):
        self.lookahead = lookahead
        self.wavelet.set_lookahead(lookahead)
        return

    # --------------------------------

    # -------------
    # Normalisation

    array_scaler = RobustScaler()
    scaler = RobustScaler()

    def update_scaler(self, data):
        if not self.array_scaler:
            self.array_scaler = RobustScaler()

        self.array_scaler.fit(data.reshape(-1, 1))

    def norm_array(self, a):
        return self.array_scaler.transform(a.reshape(-1, 1))

    def denorm_array(self, a):
        return self.array_scaler.inverse_transform(a.reshape(-1, 1)).squeeze()

    # def smooth(self, y, window):
    def smooth(self, y, window, axis=-1):
        # Apply a uniform 1d filter along the given axis
        # y_smooth = scipy.ndimage.uniform_filter1d(y, window, axis=axis, mode="nearest")
        box = np.ones(window) / window
        y_smooth = np.convolve(y, box, mode="same")
        # Hack: constrain to 3 decimal places (should be elsewhere, but convenient here)
        y_smooth = np.round(y_smooth, decimals=3)
        return np.nan_to_num(y_smooth)


    def convert_dataframe(self, dataframe: DataFrame) -> DataFrame:
        df = dataframe.copy()

        '''
        # convert date column so that it can be scaled.
        if "date" in df.columns:
            dates = pd.to_datetime(df["date"], utc=True)
            df["date"] = dates.astype("int64")

        df.fillna(0.0, inplace=True)

        if "date" in df.columns:
            df.set_index("date")
            df.reindex()

        '''
        # print(f'    norm_data:{self.norm_data}')
        if self.norm_data:
            # scale the dataframe
            self.scaler.fit(df)
            df = pd.DataFrame(self.scaler.transform(df), columns=df.columns)


        df.fillna(0.0, inplace=True)

        return df
    # -------------

    # builds a numpy array of coefficients
    def build_coefficient_table(self, start, end):

        # print(f'start:{start} end:{end} self.win_size:{self.win_size}')


        # lazy initialisation of vars (so thatthey can be changed in subclasses)
        if self.wavelet is None:
           self.wavelet = Wavelets.make_wavelet(self.wavelet_type)

        self.wavelet.set_lookahead(self.lookahead)

        if self.forecaster is None:
            self.forecaster = Forecasters.make_forecaster(self.forecaster_type)

        # if forecaster does not require pre-training, then just set training length to 0
        if not self.forecaster.requires_pretraining():
            print("    INFO: Training not required. Setting train_max_len=0")
            self.train_max_len = 0
            self.train_min_len = 0
            self.training_required = False

        if self.wavelet is None:
            print('    **** ERR: wavelet not specified')
            return

        self.forecaster.set_detrend(self.detrend_data)

        # print(f'    Wavelet:{self.wavelet_type.name} Forecaster:{self.forecaster_type.name}')

        # double check forecaster/multicolumn combo
        if (not self.single_col_prediction) and (not self.forecaster.supports_multiple_columns()):
            print('    **** WARN: forecaster does not support multiple columns')
            print('               Reverting to single column predictionss')
            self.single_col_prediction = True

        self.coeff_table = None
        self.coeff_table_offset = start

        features = None
        nrows = end - start + 1
        row_start = max(self.wavelet_size, start) - 1 # don't run until we have enough data for the transform

        c_table: np.array = []

        max_features = 0
        for row in range(row_start, end):
            # dslice = data[start:end].copy()
            win_start = max(0, row-self.wavelet_size+1)
            dslice = self.data[win_start:row+1]

            coeffs = self.wavelet.get_coeffs(dslice)
            features = self.wavelet.coeff_to_array(coeffs)
            features = np.array(features)
            flen = len(features)
            max_features = max(max_features, flen)
            c_table.append(features)

        # convert into a zero-padded fixed size array
        nrows = len(c_table)
        self.coeff_table = np.zeros((row_start+nrows, max_features), dtype=float)
        for i in range(0, nrows):
            flen = len(c_table[i]) # feature length
            # print(f'    flen:{flen} c_table[{i}]:{len(c_table[i])}')
            self.coeff_table[i+row_start-1][:flen] = np.array(c_table[i])

        # merge data from main dataframe
        self.merge_coeff_table(start, end)

        # print(f"coeff_table:{np.shape(self.coeff_table)}")
        # print(self.coeff_table[15:48])

        return

    #-------------

    # merge the supplied dataframe with the coefficient table. Number of rows must match
    def merge_coeff_table(self, start, end):


        self.coeff_num_cols = np.shape(self.coeff_table)[1]

        # if using single column prediction, no need to merge in dataframe column because they won't be used
        if self.single_col_prediction or (not self.merge_indicators):
            merged_table = self.coeff_table
            self.coeff_start_col = 0

        else:

            self.coeff_start_col = np.shape(self.curr_dataframe)[1]
            df = self.curr_dataframe.iloc[start:end]
            df_norm = self.convert_dataframe(df)
            merged_table = np.concatenate([np.array(df_norm), self.coeff_table], axis=1)

        self.coeff_table = np.nan_to_num(merged_table)

        # print(f'merge_coeff_table() self.coeff_table: {np.shape(self.coeff_table)}')
        return

    # -------------

    # generate predictions for an np array 
    def predict_data(self, predict_start, predict_end):

       # a little different than other strats, since we train a model for each column

        # check that we have enough data to run a prediction, if not return zeros
        if self.forecaster.requires_pretraining():
            min_data = self.train_min_len + self.wavelet_size + self.lookahead
        else:
            min_data = self.wavelet_size

        if predict_end < min_data-1:
            # print(f'   {predict_end} < ({self.train_min_len} + {self.wavelet_size} + {self.lookahead})')
            return np.zeros(predict_end-predict_start+1, dtype=float)

        nrows = np.shape(self.coeff_table)[0]
        ncols = np.shape(self.coeff_table)[1]
        coeff_arr: np.array = []


        # train on previous data
        # train_end = max(self.train_min_len, predict_start-1)
        train_end = min(predict_end - 1, nrows - self.lookahead - 1)
        train_start = max(0, train_end-self.train_max_len)
        results_start = train_start + self.lookahead
        results_end = train_end + self.lookahead

        # coefficient table may only be partial, so adjust start/end positions
        start = predict_start - self.coeff_table_offset
        end = predict_end - self.coeff_table_offset
        if (not self.training_required) and (self.expanding_window):
            # don't need training data, so extend prediction buffer instead
            end = predict_end - self.coeff_table_offset
            plen = 2 * self.model_window
            start = max(0, end-plen+1)

        # print(f'self.coeff_table_offset:{self.coeff_table_offset} start:{start} end:{end}')

        # get the data buffers from self.coeff_table
        if not self.single_col_prediction: # single_column version done inside loop
            predict_data = self.coeff_table[start:end]
            # predict_data = np.nan_to_num(predict_data)
            train_data = self.coeff_table[train_start:train_end]
            # results = self.coeff_table[results_start:results_end]


        # print(f'start:{start} end:{end} train_start:{train_start} train_end:{train_end} nrows:{nrows}')

        # train/predict for each coefficient individually
        for i in range(self.coeff_start_col, ncols):

            # get the data buffers from self.coeff_table
            # if single column, then just use a single coefficient
            if self.single_col_prediction:
                predict_data = self.coeff_table[start:end, i].reshape(-1,1)
                predict_data = np.nan_to_num(predict_data)
                train_data = self.coeff_table[train_start:train_end, i].reshape(-1,1)

            results = self.coeff_table[results_start:results_end, i]

            # print(f'predict_data: {predict_data.squeeze()}')
            # print(f'train_data: {train_data.squeeze()}')
            # print(f'results: {results.squeeze()}')

            # since we know we are switching data surces, disable incremental training
            self.forecaster.train(train_data, results, incremental=False)

            # get a prediction
            preds = self.forecaster.forecast(predict_data, self.lookahead)
            if preds.ndim > 1:
                preds = preds.squeeze()

            # append prediction for this column
            coeff_arr.append(preds[-1])

        # convert back to gain
        c_array = np.array(coeff_arr)
        coeffs = self.wavelet.array_to_coeff(c_array)
        preds = self.wavelet.get_values(coeffs)

        # rescale if necessary
        if self.scale_results:
            preds = self.denorm_array(preds)


        # print(f'preds[{start}:{end}] len:{len(preds)}: {preds}')
        # print(f'preds[{end}]: {preds[-1]}')
        # print('===========================')

        return preds

    # -------------

    # single prediction (for use in rolling calculation)
    # @timer
    def predict(self, gain, df) -> float:
        # Get the start and end index labels of the series
        start = gain.index[0]
        end = gain.index[-1]

        # Get the integer positions of the labels in the dataframe index
        start_row = df.index.get_loc(start)
        end_row = df.index.get_loc(end) + 1 # need to add the 1, don't know why!


        # if end_row < (self.train_max_len + self.wavelet_size + self.lookahead):
        # # if start_row < (self.wavelet_size + self.lookahead): # need buffer for training
        #     # print(f'    ({start_row}:{end_row}) y_pred[-1]:0.0')
        #     return 0.0

        # print(f'gain.index:{gain.index} start:{start} end:{end} start_row:{start_row} end_row:{end_row}')

        scale_start = max(0, len(gain)-16)

        # print(f'    coeff_table: {np.shape(self.coeff_table)} start_row: {start_row} end_row: {end_row} ')

        self.update_scaler(np.array(gain)[scale_start:])

        y_pred = self.predict_data(start_row, end_row)
        # print(f'    ({start_row}:{end_row}) y_pred[-1]:{y_pred[-1]}')
        return y_pred[-1]

    # -------------

    @timer
    def rolling_predict(self, data):

        if self.forecaster.requires_pretraining():
            min_data = self.train_min_len + self.model_window + self.lookahead
        else:
            min_data = self.model_window

        start = 0
        end = min_data - 1

        x = np.nan_to_num(np.array(data))
        preds = np.zeros(len(x), dtype=float)

        while end < len(x):

            # print(f'    start:{start} end:{end} train_max_len:{self.train_max_len} model_window:{self.model_window} min_data:{min_data}')
            if end < (min_data-1):
                start = start + 1
                end = end + 1
                continue

            scale_start = max(0, start-self.scale_len)
            scale_end = max(scale_start+self.scale_len, start)
            self.update_scaler(np.array(data)[scale_start:scale_end])

            forecast = self.predict_data(start, end)
            # if start < window_size:
            #     flen = len(forecast)
            #     preds[end-flen:end] = forecast
            # else:
            #     preds[end] = forecast[-1]
            preds[end] = forecast[-1]

            if (end == self.wavelet_size) or (end == self.wavelet_size+1) or (end == self.wavelet_size+2):
                # print(f'    {i}: coeff_array:{np.array(coeff_array)}')
                print(f'   rolling_predict {end}: self.data[{end+self.lookahead}]:{self.data[end+self.lookahead]}')
                print(f'   rolling_predict {end}: forecast[-1]:{forecast[-1]}')

            start = start + 1
            end = end + 1

        preds = preds.clip(min=-5.0, max=5.0)
        return preds

    # -------------
    # convert self.coeff_table back into a waveform (for debug)
    def rolling_coeff_table(self):
        # nrows = np.shape(self.coeff_table)[0]
        nrows = len(self.coeff_table)

        preds = np.zeros(nrows, dtype=float)

        for i in range(nrows):
            row = self.coeff_table[i]
            # print(f'    i:{i} row:{np.shape(row)}')
            # get the coefficient array from this row
            N = int(self.coeff_num_cols) # number of coeffs
            coeff_array = np.zeros(N, dtype=float)
            coeff_array = row[self.coeff_start_col:self.coeff_start_col+N]
            # print(f'    N:{N} dlen:{dlen} clen:{np.shape(coeff_array)} coeff_array:{coeff_array}')

            # convert to coefficients and get the reconstructed data
            coeffs = self.wavelet.array_to_coeff(np.array(coeff_array))
            values = self.wavelet.get_values(coeffs)
            if (i == self.wavelet_size) or (i == self.wavelet_size+1):
                # print(f'    {i}: coeff_array:{np.array(coeff_array)}')
                print(f'   rolling_coeff_table {i}: data[{i}]:{self.data[i]}')
                print(f'   rolling_coeff_table {i}: values[-1]:{values[-1]}')
            preds[i] = float(values[-1])

        return preds
# --------------------------------

# Main code

# test data taken from real run
test_data = [  0.02693603,  0.78708102,  0.29854797,  0.27140725, -0.08078632, -0.08078632,
 -0.88864952, -0.56550424, -0.06764984,  0.10826905, -0.24255491, -0.24255491,
 -0.06792555, -1.78740691, -1.23206066, -1.37893741, -1.82358503, -2.90422802,
 -1.98477433, -0.59285813, -0.87731323, -1.27484578, -1.41717116,  0.01391208,
 -0.29126214,  0.13869626,  0.        , -0.15273535,  0.36287509,  0.02782028,
  0.1391014 ,  0.20775623, -0.58083253, -0.61187596, -0.77875122, -0.77875122,
  0.12501736, -0.3731859 ,  0.26429267,  0.85350497,  1.02312544,  1.02312544,
  0.        ,  0.        ,  0.        ,  0.        , -0.15260821, -0.15260821,
  0.16648169,  0.16648169,  0.16648169, -0.84628191, -0.69473392, -0.69473392,
 -0.47091413, -0.47091413, -0.77562327,  0.08395131, -0.30782146, -0.43374843,
 -0.97411634, -0.79320902, -0.48855388, -0.95065008, -0.29473684, -0.16863406,
  0.14052839, -0.04208164,  0.04208164,  0.57868737,  0.30968468, -0.16891892,
 -0.64552344, -0.98231827, -0.75715087, -1.24894752, -1.15071569, -0.535815,
 -0.36723164, -0.02834467,  0.25430913,  2.23106437,  2.82509938,  1.57357528,
  1.57357528,  1.31840091,  0.62006764, -0.88963025, -0.86980533, -0.58618283,
 -0.58618283, -0.76955366,  0.09803922, -0.09817672, -0.79387187, -0.02807806,
 -0.02807806,  0.40891145, -0.363789 , -0.02807806, -0.02807806,  0.,
  0.3932032 ,  0.3932032 ,  0.61789075,  0.82853532,  1.33408229,  0.983008,
  0.74136243,  0.74136243,  0.51639916,  0.30640669, -0.1940133 ,  0.91781393,
  1.55512358,  1.11080255,  1.0413774 ,  1.0413774 ,  0.6942516 ,  1.01970511,
 -0.36915505,  1.11233178,  1.2367734 ,  1.26425725,  0.20683949, -0.19096985,
  0.60381501, -0.47534972 ]

# Create some random data


num_samples = 512
# np.random.seed(42)
# f1 = np.random.randn()
# np.random.seed(43)
# f2 = np.random.randn()
# np.random.seed(44)
# f3 = np.random.randn(num_samples)

# X = np.arange(num_samples)  # 100 data points
# gen_data = f1 * np.sin(0.5*X) + f2 * np.cos(0.5*X) + f3 * 0.3

# gen_data should be easier to model (use for debug), test_data is realistic
# data = gen_data
# data = np.array(gen_data)
# data = np.concatenate((test_data, test_data, test_data, test_data), dtype=float)

data = np.load('test_data.npy')

# data = StandardScaler().fit_transform(data.reshape(-1,1)).reshape(-1)
# data = RobustScaler().fit_transform(data.reshape(-1,1)).reshape(-1)
# data = MinMaxScaler().fit_transform(data.reshape(-1,1)).reshape(-1)


# put the data into a dataframe
# dataframe = pd.DataFrame(data, columns=["gain"])
# dates = pd.date_range(start="2023-01-01", periods=len(data), freq="5m")
# dataframe = pd.DataFrame(data, columns=["gain"], index=dates)
dataframe = pd.DataFrame(data, columns=["gain"])

lookahead = 6

wlist = [
    Wavelets.WaveletType.MODWT,
    # Wavelets.WaveletType.SWT,
    # Wavelets.WaveletType.WPT,
    # Wavelets.WaveletType.FFT,
    # Wavelets.WaveletType.HFFT,
    # Wavelets.WaveletType.DWT,
    # Wavelets.WaveletType.DWTA,
    ]
flist = [
    # Forecasters.ForecasterType.NULL, # use this to show effect of wavelet alone
    # Forecasters.ForecasterType.EXPONENTAL,
    # Forecasters.ForecasterType.ETS,
    # Forecasters.ForecasterType.SIMPLE_EXPONENTAL,
    # Forecasters.ForecasterType.HOLT,
    # Forecasters.ForecasterType.SS_EXPONENTAL,
    # Forecasters.ForecasterType.AR,
    # Forecasters.ForecasterType.ARIMA,
    # Forecasters.ForecasterType.THETA,
    # Forecasters.ForecasterType.LINEAR,
    # Forecasters.ForecasterType.QUADRATIC,
    # Forecasters.ForecasterType.FFT_EXTRAPOLATION,
    # Forecasters.ForecasterType.MLP,
    # Forecasters.ForecasterType.KMEANS,
    Forecasters.ForecasterType.PA,
    # Forecasters.ForecasterType.SGD,
    # Forecasters.ForecasterType.SVR,
    # Forecasters.ForecasterType.GB,
    # Forecasters.ForecasterType.HGB,
    # Forecasters.ForecasterType.LGBM,
    # Forecasters.ForecasterType.XGB
]

# llist = [ 16, 32, 36, 64 ]
llist = [ 32 ]
marker_list = [ '.', 'o', 'v', '^', '<', '>', 'p', '*', 'h', 'H', 'D', 'd', 'P', 'X' ]
num_markers = len(marker_list)
mkr_idx = 0


# Plot the original data

dataframe['gain_shifted'] = dataframe['gain'].shift(-lookahead)
# ax = dataframe['gain'].plot(label='Original', marker="x", color="black")
ax = dataframe['gain_shifted'].plot(label='Original (shifted)', marker="x", color="black")

for wavelet_type in wlist:
    for forecaster_type in flist:
        for length in llist:
            label = wavelet_type.name + "/" + forecaster_type.name + f" ({length})"
            print(label)

            predictor = WaveletPredictor()
            predictor.set_wavelet_type(wavelet_type)
            predictor.set_wavelet_len(length)
            predictor.set_forecaster_type(forecaster_type)
            predictor.set_data(dataframe)
            predictor.set_lookahead(lookahead)


            # # Plot the coeff_table reconstruction
            # dataframe["coeff_table"] = predictor.rolling_coeff_table()
            # dataframe["coeff_table"].plot(ax=ax, label=label+" coeff_table", linestyle="dashed", marker=marker_list[mkr_idx])
            # mkr_idx = (mkr_idx + 1) % num_markers

            dataframe["predicted_gain"] = predictor.rolling_predict(dataframe["gain"])
            dataframe["predicted_gain"].plot(ax=ax, label=label, linestyle="dashed", marker=marker_list[mkr_idx])
            mkr_idx = (mkr_idx + 1) % num_markers

plt.legend()
plt.show()