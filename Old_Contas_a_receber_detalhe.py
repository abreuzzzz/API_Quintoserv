import os
import json
import pandas as pd
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from googleapiclient.discovery import build
from google.oauth2 import service_account
import time
import random
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ===================== Autenticação Google =====================
json_secret = os.getenv("GDRIVE_SERVICE_ACCOUNT")
credentials_info = json.loads(json_secret)
credentials = service_account.Credentials.from_service_account_info(
    credentials_info,
    scopes=["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets"]
)

drive_service = build("drive", "v3", credentials=credentials)
sheets_service = build("sheets", "v4", credentials=credentials)

# ===================== Buscar arquivos no Drive =====================
folder_id = "1_kJtBN_cr_WpND1nF3WtI5smi3LfIxNy"
sheet_input_name = "Financeiro_contas_a_receber_Bluefields"
sheet_output_name = "Detalhe_centro_recebimento"

def get_file_id(name):
    query = f"name='{name}' and '{folder_id}' in parents and trashed=false"
    result = drive_service.files().list(q=query, spaces="drive", fields="files(id, name)").execute()
    files = result.get("files", [])
    if not files:
        raise FileNotFoundError(f"Arquivo '{name}' não encontrado na pasta especificada.")
    return files[0]["id"]

input_sheet_id = get_file_id(sheet_input_name)
output_sheet_id = get_file_id(sheet_output_name)

# ===================== Leitura do Google Sheets =====================
result = sheets_service.spreadsheets().values().get(
    spreadsheetId=input_sheet_id,
    range="A:Z"
).execute()

values = result.get('values', [])
df_base = pd.DataFrame(values[1:], columns=values[0])
ids = df_base["financialEvent.id"].dropna().unique()

print(f"📥 Planilha carregada com {len(ids)} IDs únicos.")

# ===================== Sessão HTTP com retry automático =====================
def create_session_with_retry():
    """Cria sessão com retry automático e exponential backoff"""
    session = requests.Session()
    retry_strategy = Retry(
        total=5,
        backoff_factor=2,  # 2^n segundos: 1s, 2s, 4s, 8s, 16s
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=20)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

headers = {
    'X-Authorization': '00e3b816-f844-49ee-a75e-3da30f1c2630',
    'User-Agent': 'Mozilla/5.0'
}

# ===================== Rate Limiter =====================
class RateLimiter:
    def __init__(self, max_per_second=5):
        self.max_per_second = max_per_second
        self.min_interval = 1.0 / max_per_second
        self.last_call = 0
    
    def wait(self):
        """Espera o tempo necessário antes da próxima requisição"""
        elapsed = time.time() - self.last_call
        if elapsed < self.min_interval:
            sleep_time = self.min_interval - elapsed + random.uniform(0, 0.1)  # Jitter
            time.sleep(sleep_time)
        self.last_call = time.time()

rate_limiter = RateLimiter(max_per_second=5)

# ===================== Função para extrair campos aninhados =====================
def extract_fields(item):
    resultado = []
    base_id = item.get("id")
    categories = item.get("categoriesRatio", [])

    for cat in categories:
        linha = {"id": base_id}
        for k, v in cat.items():
            if k == "costCentersRatio":
                for i, centro in enumerate(v):
                    for ck, cv in centro.items():
                        linha[f"categoriesRatio.costCentersRatio.{i}.{ck}"] = cv
            else:
                linha[f"categoriesRatio.{k}"] = v
        resultado.append(linha)
    
    # Se não houver categoriesRatio, criar linha com ID
    if not categories:
        resultado.append({"id": base_id})

    return resultado

# ===================== Coleta com controle de taxa e retry =====================
def fetch_detail(fid, session):
    url = f"https://services.contaazul.com/contaazul-bff/finance/v1/financial-events/{fid}/summary"
    rate_limiter.wait()  # Controle de taxa
    
    try:
        response = session.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            return extract_fields(response.json())
        elif response.status_code == 429:
            print(f"⏳ Rate limit no ID {fid}, aguardando...")
            time.sleep(5 + random.uniform(0, 2))
            return None
        else:
            print(f"❌ Erro no ID {fid}: {response.status_code}")
    except Exception as e:
        print(f"⚠️ Falha no ID {fid}: {e}")
    return None

print("🚀 Iniciando requisições paralelas com rate limiting...")

todos_detalhes = []
session = create_session_with_retry()

# Reduzir workers para 3 threads (mais estável e evita 429)
with ThreadPoolExecutor(max_workers=3) as executor:
    futures = [executor.submit(fetch_detail, fid, session) for fid in ids]
    for idx, f in enumerate(as_completed(futures), 1):
        resultado = f.result()
        if resultado:
            todos_detalhes.extend(resultado)
        if idx % 50 == 0:
            print(f"⏳ Processados {idx}/{len(ids)} IDs...")

print(f"✅ Coleta finalizada com {len(todos_detalhes)} registros.")

# ===================== Enviar dados usando batchUpdate (otimizado) =====================
df_detalhes = pd.DataFrame(todos_detalhes)

# Preparar dados
all_data = [df_detalhes.columns.tolist()] + df_detalhes.fillna("").astype(str).values.tolist()

# **OTIMIZAÇÃO**: Usar batchUpdate com uma única requisição
print(f"📊 Enviando {len(all_data)} linhas em lote único (otimizado)...")
try:
    sheets_service.spreadsheets().values().batchUpdate(
        spreadsheetId=output_sheet_id,
        body={
            "valueInputOption": "RAW",
            "data": [
                {
                    "range": "A1",
                    "values": all_data
                }
            ]
        }
    ).execute()
    print(f"✅ {len(all_data)} linhas enviadas com sucesso em uma única operação!")
    
except Exception as e:
    print(f"❌ Erro no envio em lote único: {e}")
    print("🔄 Tentando com método de fallback...")
    
    # Fallback: limpar e enviar em chunks maiores
    sheets_service.spreadsheets().values().clear(
        spreadsheetId=output_sheet_id,
        range="A:Z"
    ).execute()
    
    # Enviar em chunks de 5000 linhas
    chunk_size = 5000
    for i in range(0, len(all_data), chunk_size):
        chunk = all_data[i:i + chunk_size]
        start_row = i + 1
        
        sheets_service.spreadsheets().values().update(
            spreadsheetId=output_sheet_id,
            range=f"A{start_row}",
            valueInputOption="RAW",
            body={"values": chunk}
        ).execute()
        print(f"📊 Chunk {i//chunk_size + 1} enviado: {len(chunk)} linhas")
        time.sleep(1)  # Pausa entre chunks

print("📊 Dados atualizados na planilha com sucesso.")
