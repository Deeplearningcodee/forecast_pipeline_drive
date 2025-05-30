#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
– Télécharge les inputs depuis Google Drive via gdown
– Calcule coefficients & métriques
– Exporte dans output/
– Upload headless vers votre dossier Drive avec PyDrive2,
  en chargeant mycreds.txt depuis l’ENV GDRIVE_MYCREDS
"""
import os, logging
from pathlib import Path

import gdown
import pandas as pd
import numpy as np
import statsmodels.api as sm
from sklearn.metrics import r2_score, mean_absolute_error, mean_absolute_percentage_error
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

# ----------- CONFIG ----------------
INPUT_FILES = {
  'historique_corrige_drive.csv': '1St2k0ZUNEP-UesbedluVKGUSGIAEwWGu',
  'Calendrier.csv':                '1X-szdCNtW62MWyOpn21wDkxinx_1Ijp9',
  'Paye_calendrier.csv':           '1yjAHMX7h3U66Tx37rgBIK2z7OWyxRGKO',
}
UPLOAD_FOLDER_ID = '1VC1Q-hyJe1CTiHGMXC_3e13UVIIOfaJ3'

WORKDIR     = Path('.')
OUTPUT_DIR  = WORKDIR/'output'
OUTPUT_DIR.mkdir(exist_ok=True)

HIST_FILE   = WORKDIR/'historique_corrige_drive.csv'
CAL_FILE    = WORKDIR/'Calendrier.csv'
PAYE_FILE   = WORKDIR/'Paye_calendrier.csv'

OUT_COEF_L  = OUTPUT_DIR/'coefficients_drive.csv'
OUT_COEF_W  = OUTPUT_DIR/'coefficients_drive_wide.csv'
OUT_METRICS = OUTPUT_DIR/'metrics_drive.csv'

ZONE_MAP = {
  'PPC_Aulnay':'C','PPC_SQF':'A',
  'PPC_Solo_Antibes':'B','PPC_Solo_Aix':'B','PPC_LPP':'C',
}

# ----------- HEADLESS AUTH (once) -------------
# If GDRIVE_MYCREDS is set, overwrite/create mycreds.txt at startup
if os.getenv('GDRIVE_MYCREDS'):
    with open('mycreds.txt','w') as f:
        f.write(os.environ['GDRIVE_MYCREDS'])

def get_drive() -> GoogleDrive:
    gauth = GoogleAuth()
    gauth.LoadCredentialsFile('mycreds.txt')
    if gauth.access_token_expired:
        gauth.Refresh()
    gauth.SaveCredentialsFile('mycreds.txt')
    return GoogleDrive(gauth)

# ----------- UTIL ----------------
def _yearweek(d):
    iso = d.isocalendar()
    return f"{iso.year}-{iso.week:02d}"

def download_inputs():
    for name, fid in INPUT_FILES.items():
        url = f"https://drive.google.com/uc?id={fid}"
        logging.info(f"Downloading {name}")
        gdown.download(url, str(WORKDIR/name), quiet=False)

# ----------- LOAD & BUILD ------------
def load_drive_history():
    df = pd.read_csv(
      HIST_FILE, sep=';', encoding='latin-1', dayfirst=True,
      parse_dates=['DATE_RETRAIT'],
      usecols=['DATE_RETRAIT','NOM_SITE_PREP','POTENTIEL_CDE_CORRIGE']
    ).rename(columns={
      'DATE_RETRAIT':'date','NOM_SITE_PREP':'site','POTENTIEL_CDE_CORRIGE':'commandes'
    })
    df['commandes'] = ( df['commandes']
      .astype(str).str.replace(',','.')
      .pipe(pd.to_numeric,errors='coerce')
      .fillna(0).astype(int)
    )
    df['ID_SEM'] = df['date'].apply(_yearweek)
    return df[['site','ID_SEM','commandes']]

def load_calendar():
    cal = pd.read_csv(
      CAL_FILE, sep=';', encoding='latin-1', dayfirst=True,
      parse_dates=['JOUR'],
      usecols=['JOUR','SEMAINE','TYPE_SEM_ZONE',
               'SEM_FERIE','SEM_PRE_FERIE','SEM_POST_FERIE','TYPE_SEM_FERIE']
    ).rename(columns={'JOUR':'date','SEMAINE':'week'})
    for c in ['SEM_FERIE','SEM_PRE_FERIE','SEM_POST_FERIE']:
        cal[c] = cal[c].map({'VRAI':1,'FAUX':0}).fillna(0).astype(int)
    paye = pd.read_csv(
      PAYE_FILE, sep=';', encoding='latin-1', dayfirst=True,
      parse_dates=['JOUR'], usecols=['JOUR','TYPE_SEM_PAYE_FCT']
    ).rename(columns={'JOUR':'date'})
    paye['TYPE_SEM_PAYE_FCT'] = paye['TYPE_SEM_PAYE_FCT'].fillna('S_NORMALE')
    cal = cal.merge(paye, on='date', how='left')
    cal['TYPE_SEM_PAYE_FCT'] = cal['TYPE_SEM_PAYE_FCT'].fillna('S_NORMALE')
    cal['ID_SEM'] = cal['date'].apply(_yearweek)
    return cal[['ID_SEM','TYPE_SEM_ZONE',
                'SEM_FERIE','SEM_PRE_FERIE','SEM_POST_FERIE',
                'TYPE_SEM_FERIE','TYPE_SEM_PAYE_FCT']]

def build_drive_dataset():
    hist = load_drive_history()
    cal  = load_calendar()
    df   = hist.merge(cal, on='ID_SEM', how='left')
    df['ZONE_SCOLAIRE'] = df['site'].map(ZONE_MAP).fillna('C')
    return df

# ----------- PIPELINE -------------
def run_pipeline():
    logging.info("Starting pipeline")
    download_inputs()
    df = build_drive_dataset()
    vars_cal = ['TYPE_SEM_ZONE','SEM_FERIE','SEM_PRE_FERIE','SEM_POST_FERIE','TYPE_SEM_FERIE','TYPE_SEM_PAYE_FCT']

    metrics, results = [], []
    for site, grp in df.groupby('site'):
        grp = grp.sort_values('ID_SEM').reset_index(drop=True)
        grp['t'] = np.arange(len(grp))

        det = sm.OLS(grp['commandes'].astype(float), sm.add_constant(grp['t'])).fit()
        y_det = (grp['commandes'] - det.predict(sm.add_constant(grp['t']))).astype(float)

        X = pd.get_dummies(grp[vars_cal], drop_first=True, dtype=float)
        X = sm.add_constant(X)
        mod = sm.OLS(y_det, X).fit()

        y_pred = det.predict(sm.add_constant(grp['t'])) + mod.predict(X)
        y_true = grp['commandes']
        mask24 = grp['ID_SEM'].str.startswith('2024-')
        yt24, yp24 = y_true[mask24], y_pred[mask24]

        metrics.append({
          'site': site,
          'r2_insample': r2_score(y_true, y_pred),
          'mae_insample': mean_absolute_error(y_true, y_pred),
          'mape_insample': mean_absolute_percentage_error(y_true, y_pred),
          'r2_2024':    (r2_score(yt24, yp24) if len(yt24)>0 else np.nan),
          'mae_2024':   (mean_absolute_error(yt24, yp24) if len(yt24)>0 else np.nan),
          'mape_2024':  (mean_absolute_percentage_error(yt24, yp24) if len(yt24)>0 else np.nan),
        })

        coefs = mod.params.reset_index()
        coefs.columns = ['variable','coef']
        coefs['site'] = site
        results.append(coefs)

    df_coefs = pd.concat(results, ignore_index=True)[['site','variable','coef']]
    df_coefs.to_csv(OUT_COEF_L, sep=';', decimal=',', encoding='latin-1', index=False)

    df_wide = df_coefs.pivot(index='site', columns='variable', values='coef').reset_index()
    df_wide.to_csv(OUT_COEF_W, sep=';', decimal=',', encoding='latin-1', index=False)

    pd.DataFrame(metrics).to_csv(OUT_METRICS, sep=';', decimal=',', encoding='latin-1', index=False)

    logging.info("Outputs written to 'output/'")

    drive = get_drive()
    for path in [OUT_COEF_L, OUT_COEF_W, OUT_METRICS]:
        logging.info(f"Uploading {path.name}…")
        f = drive.CreateFile({'title': path.name, 'parents':[{'id': UPLOAD_FOLDER_ID}]})
        f.SetContentFile(str(path))
        f.Upload()
        logging.info(f"Uploaded → {path.name}")

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    run_pipeline()
