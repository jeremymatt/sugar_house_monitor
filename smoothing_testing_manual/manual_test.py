import pandas as pd
from scipy.optimize import curve_fit

df = pd.read_csv('brookside.csv')

window_size = 50

def line_func(x,m,b):
    return m*x+b

