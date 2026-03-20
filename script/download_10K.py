import os
import shutil
from sec_edgar_downloader import Downloader
import pandas as pd
import requests
import csv
#SEC User-Agent identification and downloader object
dl_folder = '/home/aicha/PycharmProjects/AI-Infrastructure-investment-project/raw_filings'
dl = Downloader('Koureich Aïcha', 'aicha.koureich@telecom-sudparis.eu', download_folder=dl_folder)
headers_sec = {'User-Agent': 'Koureich Aïcha (aicha.koureich@telecom-sudparis.eu)'}

#Finding the cik
print('Getting the cik list from the SEC')
cik_find = requests.get('https://www.sec.gov/files/company_tickers.json', headers=headers_sec)
#Transform raw sec data in python data
data_sec = cik_find.json()

#Transform the ticker in cik for renaming purpose
ticker_to_cik = {}

#List for the company which cik wasnt found
missing_cik = []

for data in data_sec.values():
    tk = data['ticker']
    cik = str(data['cik_str']).zfill(10) #CIK standard format
    ticker_to_cik[tk] = cik

#Extract information from the Excel and renaming the files if cik found
raw_data = pd.read_excel('/home/aicha/Documents/TSP/2A/Cassiopée/sp1500_list.xls')

max_companies = 11

companies_count = set() #Created to limit the number of companies downloaded
for row_index, row in raw_data.iterrows():
    year = int(row['fyear'])
    tk = row['tic']
    company_name = row['conml']

    if len(companies_count) >= max_companies and tk not in companies_count:
        print(f"Total number of companies reached: {max_companies}, end of script check raw fillings directory")
        break
    else :
        companies_count.add(tk)

    print(f'\n----[{row_index}] Searching the CIK for the company: {company_name}, tk: {tk} in year: {year} ----')
    cik = ticker_to_cik.get(tk)

    if cik:

        # Target file name and path
        file_new_name = f"{cik}_{tk}_FY{year}_10K.html"
        # Create a subdirectory in raw filings just for organisation
        company_dir = os.path.join(dl_folder, cik)
        os.makedirs(company_dir, exist_ok=True)
        file_new_path = os.path.join(company_dir, file_new_name)

        if os.path.exists(file_new_path):
            print(f"[{row_index}] CIK {cik} already downloaded and renamed")
            continue

        print(f'[{row_index}] Downloading the 10-K for the company: {company_name}, cik: {cik}, in year: {year}')
        dl.get('10-K', cik, after=f'{year}-01-01', before=f'{year+1}-07-01', limit=1, download_details=True)
        #target_filename = f"{cik}_{tk}_FY{year}_10K.pdf" ca renomme pas direct en fait le download fait un peu a sa sauce

        #This is where the download object initially put the file
        source_folder = os.path.join(dl_folder, 'sec-edgar-filings', cik, '10-K')
        #We look for the .html file
        for root, dirs, files in os.walk(source_folder):
            for file in files:
                if file.endswith(".html"):
                    file_old_path = os.path.join(root, file)

                    #Moving the file directly in raw filings directory
                    shutil.move(file_old_path, file_new_path)
                    print(f"File {file_new_name} successfully downloaded and renamed")
        if os.path.exists(os.path.join(dl_folder, 'sec-edgar-filings')):
            shutil.rmtree(os.path.join(dl_folder, 'sec-edgar-filings'))
    else:
        print(f'[{row_index}] Error CIK not found for ticker {tk}')
        missing_cik.append({'ticker':tk, 'company': company_name, 'year': year})
if missing_cik:
    missing = pd.DataFrame(missing_cik)
    missing.to_csv('missingCIK.csv', index = False)
    print(f"List of missing CIKs. Total: {len(missing_cik)}")