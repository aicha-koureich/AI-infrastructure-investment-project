import os
import shutil
import zipfile
from sec_edgar_downloader import Downloader
import pandas as pd
import requests
import time
import csv

"To run this program you need access to the dri, then add it to your **My Drive** section and mount the folder to your computer"
"On linux, remount to the drive with q and then: 1) fusermount -u ~/google_drive "
"                                                2) rclone mount gdrive: ~/google_drive --vfs-cache-mode full --dir-cache-time 10s &"

MAX_COMPANIES = 4000 #Created to limit the number of companies downloaded
BATCH_SIZE = 20
companies_count = set() 
batch_number = 1
current_batch_count = 0
current_batch_log = [] #for progress file
missing_cik = []
missing_10K = []
#Set to start at last checkpoint 
processed_set = set() 
failed_set = set()

# Temmporary Local folder to download some raw filings 
current_dir = os.path.dirname(os.path.abspath(__file__))
local_temp_folder = os.path.join(current_dir, 'temp_download')
os.makedirs(local_temp_folder, exist_ok=True)

#We send files to a shared drive
drive_folder = os.path.expanduser('~/google_drive/AI_Infrastructure_Investment_Project/raw_filings')

#Progress csv file
#success
progress_file = os.path.join(drive_folder, 'log_progress.csv')

if os.path.exists(progress_file):
    df_prog = pd.read_csv(progress_file)
    processed_set = set(zip(df_prog['ticker'], df_prog['year']))
    if not df_prog.empty:
        batch_number = df_prog['batch_id'].max() +1
    print(f"ALready {len(processed_set)} files zipped in the Drive.")

#failures
if os.path.exists('missingCIK.csv'):
    df_cik = pd.read_csv('missingCIK.csv')
    failed_set.update(set(zip(df_cik['ticker'], df_cik['year'])))
if os.path.exists('missing10K.csv'):
    df_10K = pd.read_csv('missing10K.csv')
    failed_set.update(set(zip(df_10K['ticker'], df_10K['year'])))
print(f"Number of unfound files/cik: {len(failed_set)}")

#SEC User-Agent identification
dl = Downloader('Koureich Aïcha', 'aicha.koureich@telecom-sudparis.eu', download_folder=local_temp_folder)
headers_sec = {'User-Agent': 'Koureich Aïcha (aicha.koureich@telecom-sudparis.eu)'}

#Finding the cik
print('Getting the cik list from the SEC')
cik_find = requests.get('https://www.sec.gov/files/company_tickers.json', headers=headers_sec)
#Transform raw sec data in python data
data_sec = cik_find.json()

#Transform the ticker in cik for renaming purpose
ticker_to_cik = {}

#Zip function, companies stored by batch
def create_zip(source_dir, output_zip_path):
    with zipfile.ZipFile(output_zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(source_dir):
                for file in files:
                    full_path = os.path.join(root, file)
                    #subfiles
                    rel_path = os.path.relpath(full_path, source_dir)
                    zipf.write(full_path, rel_path)

for data in data_sec.values():
    tk = data['ticker']
    cik = str(data['cik_str']).zfill(10) #CIK standard format
    ticker_to_cik[tk] = cik

#Extract information from the Excel 
raw_data = pd.read_excel('/home/aicha/Documents/TSP/2A/Cassiopée/sp1500_list.xls')

for row_index, row in raw_data.iterrows():
    year = int(row['fyear'])
    tk = row['tic']
    company_name = row['conml']

    if (tk, year) in processed_set or (tk, year) in failed_set:
        continue

    if len(companies_count) >= MAX_COMPANIES and tk not in companies_count:
        print(f"Total number of companies reached: {MAX_COMPANIES}, end of script check raw fillings directory")
        break

     #Compressing and sending the batch to the Drive if BATCH_SIZE reached
    if tk not in companies_count and current_batch_count >= BATCH_SIZE:
        zip_name = f"batch_{batch_number}.zip"
        local_zip_path = os.path.join(current_dir, zip_name)
        drive_zip_path = os.path.join(drive_folder, zip_name)

        print(f"Batch number{batch_number} done. Compressing...")
        create_zip(local_temp_folder, local_zip_path)
        print(f"Sending batch number: {batch_number} on the Drive...")
        shutil.move(local_zip_path, drive_zip_path)
        #Progress file update
        pd.DataFrame(current_batch_log).to_csv(progress_file, mode='a', header=not os.path.exists(progress_file), index=False)
        #Emptying the local folder
        shutil.rmtree(local_temp_folder)
        os.makedirs(local_temp_folder, exist_ok=True)

        #Resetting or updating variables
        batch_number+=1
        current_batch_count = 0
        current_batch_log = []
        print(f"Batch number: {batch_number-1} on the Drive. Starting next batch ID({batch_number})...")
        # Saving in logs now for security
        if missing_cik:
            file_exists = os.path.isfile('missingCIK.csv')
            pd.DataFrame(missing_cik).to_csv('missingCIK.csv', mode='a', index=False, header=not file_exists)            
            missing_cik = []
        if missing_10K:
            file_exists = os.path.isfile('missing10K.csv')
            pd.DataFrame(missing_10K).to_csv('missing10K.csv', mode='a', index=False, header=not file_exists)            
            missing_10K = []
            
    if tk not in companies_count :
        companies_count.add(tk)
        current_batch_count +=1
        print (f"Company count: {len(companies_count)}, Batch count: {current_batch_count}")
        

    print(f'\n----[{row_index}] Searching the CIK for the company: {company_name}, tk: {tk} in year: {year} ----')
    cik = ticker_to_cik.get(tk)
    
    if cik:

        # Target file name and path
        file_new_name = f"{cik}_{tk}_FY{year}_10K.html"
        # Create a subdirectory in raw filings just for organisation
        company_dir = os.path.join(local_temp_folder, cik)
        os.makedirs(company_dir, exist_ok=True)
        file_new_path = os.path.join(company_dir, file_new_name)

        if os.path.exists(file_new_path):
            print(f"[{row_index}] CIK {cik} already downloaded and renamed")
            #Added to the log because it is not compressed yet
            current_batch_log.append({'ticker': tk, 'year': year, 'batch_id': batch_number})
            continue

        print(f'[{row_index}] Downloading the 10-K for the company: {company_name}, cik: {cik}, in year: {year}')
        time.sleep(2) # Needed to not get blocked by the SEC
        dl.get('10-K', cik, after=f'{year}-01-01', before=f'{year+1}-07-01', limit=1, download_details=True)
        #target_filename = f"{cik}_{tk}_FY{year}_10K.pdf" ca renomme pas direct en fait le download fait un peu a sa sauce
        
        #This is where the download object initially put the file
        source_folder = os.path.join(local_temp_folder, 'sec-edgar-filings', cik, '10-K')
        #We look for the .html file
        found_html = False
        for root, dirs, files in os.walk(source_folder):
            for file in files:
                if file.endswith(".html"):
                    file_old_path = os.path.join(root, file)
                    #Moving the file directly in drive directory
                    shutil.move(file_old_path, file_new_path)
                    print(f"File {file_new_name} successfully downloaded and renamed")
                    found_html = True
                    current_batch_log.append({'ticker': tk, 'year': year, 'batch_id': batch_number})

        if not found_html:
            print(f'[{row_index}] CIK found but No 10-K for {tk} in {year}')
            missing_10K.append({'cik': cik, 'ticker': tk, 'company': company_name, 'year': year})

        if os.path.exists(os.path.join(local_temp_folder, 'sec-edgar-filings')):
            shutil.rmtree(os.path.join(local_temp_folder, 'sec-edgar-filings'))
    else:
        print(f'[{row_index}] Error CIK not found for ticker {tk}')
        missing_cik.append({'ticker': tk, 'company': company_name, 'year': year})


#If less than BATCH_SIZE companies remain for the last batch
if current_batch_count > 0:
    zip_name = f"batch_{batch_number}.zip"
    create_zip(local_temp_folder, os.path.join(drive_folder, zip_name))
    pd.DataFrame(current_batch_log).to_csv(progress_file, mode='a', header=not os.path.exists(progress_file), index=False)
    print(f"final batch done")

if missing_cik:
    file_exists = os.path.isfile('missingCIK.csv')
    pd.DataFrame(missing_cik).to_csv('missingCIK.csv', mode='a', index=False, header=not file_exists)
if missing_10K:
    file_exists = os.path.isfile('missing10K.csv')
    pd.DataFrame(missing_10K).to_csv('missing10K.csv', mode='a', index=False, header=not file_exists)
print(f"Done. List of missing CIKs: {len(missing_cik)} | List of missing 10K w/CIK: {len(missing_10K)}")