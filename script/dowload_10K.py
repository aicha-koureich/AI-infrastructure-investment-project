from sec_edgar_downloader import Downloader
import pandas as pd
import requests

#SEC User-Agent identification
dl = Downloader("Koureich Aïcha", "aicha.koureich@telecom-sudparis.eu", "/home/aicha/PycharmProjects/AI-Infrastructure-investment-project/raw_filings")

# Opening our excel
raw_data = pd.read_excel('sp_1500_list.xlsx')
line = raw_data.iterrows()

cik_find = requests.get('https://www.sec.gov/files/company_tickers.json',"Koureich Aïcha (aicha.koureich@telecom-sudparis.eu)" )
get_cik = {ticker: cik}

for row in line:

    gv = row[0]
    ticker = row[3]
    year = row[1]

