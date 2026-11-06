import os
import json
import pandas as pd
import requests
from io import BytesIO
from datetime import datetime, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ===================== Autenticar com Google APIs =====================
json_secret = os.getenv("GDRIVE_SERVICE_ACCOUNT")
credentials_info = json.loads(json_secret)
scopes = ["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets"]
credentials = service_account.Credentials.from_service_account_info(credentials_info, scopes=scopes)
drive_service = build("drive", "v3", credentials=credentials)
sheets_service = build("sheets", "v4", credentials=credentials)

# ===================== Configurações =====================
CONTA_AZUL_TOKEN = os.getenv("CONTA_AZUL_TOKEN", "3ee0ec6e-9588-47d4-addb-6fda7f4199ec")
WORKER_URL = os.getenv("CONTA_AZUL_WORKER_URL", "https://ca.souza-brunoabreu.workers.dev")

export_url = "https://services.contaazul.com/finance-pro-reports/v1/financial-statement-view/export"


def call_conta_azul_proxy(payload: dict):
    """Envia a requisição pela rota do Cloudflare Worker para contornar 403/cors."""
    request_body = {
        "url": export_url,
        "method": "POST",
        "headers": {
            "X-Authorization": CONTA_AZUL_TOKEN,
            "Content-Type": "application/json",
            "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, application/octet-stream, application/json, */*",
            "Origin": "https://pro.contaazul.com",
            "Referer": "https://pro.contaazul.com/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5.2 Safari/605.1.15"
        },
        "body": payload,
    }

    response = requests.post(WORKER_URL, json=request_body, timeout=120)
    response.raise_for_status()
    content = response.content

    if not content or not content.startswith(b"PK"):
        preview = content[:300]
        raise ValueError(
            f"Resposta inválida do worker. status={response.status_code}, "
            f"content_type={response.headers.get('Content-Type')}, preview={preview!r}"
        )

    # Detecta corrupção de binário: bytes inválidos UTF-8 substituídos por U+FFFD
    # (EF BF BD). Isso acontece quando o worker usa upstream.text() em vez de
    # arrayBuffer(), destruindo o ZIP/XLSX.
    if b"\xef\xbf\xbd" in content:
        raise ValueError(
            "Arquivo XLSX corrompido: o worker retornou bytes U+FFFD (EF BF BD). "
            "Corrija o worker para usar upstream.arrayBuffer() em vez de upstream.text()."
        )

    return content


# Lista de status para processar
status_list = ["ACQUITTED", "PARTIAL", "PENDING", "LOST", "RENEGOTIATED", "CONCILIATED", "OVERDUE"]

# ===================== Baixar e consolidar arquivos XLSX =====================
print("🔄 Iniciando download dos arquivos XLSX para cada status...")

all_dataframes = []

for status_atual in status_list:
    print(f"\n📥 Baixando dados para status: {status_atual}")

    payload = {
        "dateFrom": None,
        "dateTo": None,
        "quickFilter": "ALL",
        "search": "",
        "status": [status_atual],
        "type": ["REVENUE"]
    }

    try:
        response_content = call_conta_azul_proxy(payload)

        xlsx_content = BytesIO(response_content)
        df = pd.read_excel(xlsx_content)
        df['status'] = status_atual

        print(f"  ✅ {len(df)} registros baixados para {status_atual}")
        all_dataframes.append(df)

    except requests.exceptions.RequestException as e:
        print(f"  ⚠️ Erro ao baixar dados para {status_atual}: {e}")
        continue
    except Exception as e:
        print(f"  ⚠️ Erro ao processar arquivo XLSX para {status_atual}: {e}")
        continue

# ===================== Consolidar todos os DataFrames =====================
if not all_dataframes:
    raise Exception("❌ Nenhum dado foi baixado com sucesso!")

print(f"\n🔄 Consolidando {len(all_dataframes)} arquivos...")
df_consolidado = pd.concat(all_dataframes, ignore_index=True)

if 'id' in df_consolidado.columns:
    df_consolidado = df_consolidado.drop_duplicates(subset=['id'], keep='first')
    print(f"📋 Total de registros únicos após remoção de duplicatas: {len(df_consolidado)}")
else:
    print(f"📋 Total de registros consolidados: {len(df_consolidado)}")

# ===================== MAPEAR CONCILIATED PARA ACQUITTED =====================
print(f"\n🔄 Mapeando status CONCILIATED para ACQUITTED...")
mask_conciliated = df_consolidado['status'] == 'CONCILIATED'
total_conciliated = mask_conciliated.sum()
df_consolidado.loc[mask_conciliated, 'status'] = 'ACQUITTED'
print(f"  ✅ {total_conciliated} registros CONCILIATED convertidos para ACQUITTED")

# ===================== Criar coluna "Data do último pagamento" =====================
print(f"\n🔄 Criando coluna 'Data do último pagamento' baseada em Situação e Data movimento...")

if 'Situação' in df_consolidado.columns and 'Data movimento' in df_consolidado.columns:
    df_consolidado['Data do último pagamento'] = None
    
    mask = df_consolidado['Situação'].isin(['Quitado', 'Conciliado'])
    df_consolidado.loc[mask, 'Data do último pagamento'] = df_consolidado.loc[mask, 'Data movimento']
    
    registros_preenchidos = mask.sum()
    print(f"  ✅ Coluna 'Data do último pagamento' criada com {registros_preenchidos} registros preenchidos")
else:
    print(f"  ⚠️ AVISO: Colunas 'Situação' e/ou 'Data movimento' não encontradas!")

# ===================== Atualizar status PENDING para OVERDUE =====================
print(f"\n🔄 Verificando status PENDING com data vencida...")

ontem = datetime.now() - timedelta(days=1)
ontem = ontem.replace(hour=0, minute=0, second=0, microsecond=0)

col_vencimento = "Data do último pagamento"

if col_vencimento in df_consolidado.columns:
    df_consolidado[col_vencimento] = pd.to_datetime(df_consolidado[col_vencimento], format='%d/%m/%Y', errors='coerce', dayfirst=True)
    mask_update = (df_consolidado['status'] == 'PENDING') & (df_consolidado[col_vencimento] <= ontem)
    total_atualizados = mask_update.sum()
    df_consolidado.loc[mask_update, 'status'] = 'OVERDUE'
    print(f"  ✅ {total_atualizados} registros PENDING atualizados para OVERDUE")
else:
    print(f"  ⚠️ AVISO: Coluna '{col_vencimento}' não encontrada!")

# ===================== Converter colunas datetime para string =====================
print(f"\n🔄 Convertendo colunas de data para string...")

datetime_columns = df_consolidado.select_dtypes(include=['datetime64']).columns.tolist()

for col in datetime_columns:
    df_consolidado[col] = df_consolidado[col].dt.strftime('%d/%m/%Y')
    print(f"  ✅ Coluna '{col}' convertida para string")

# ===================== Renomear colunas conforme especificação =====================
print(f"\n🔄 Renomeando colunas...")

colunas_renomear = {
    "Data original de vencimento": "dueDate",
    "Data de competência": "financialEvent.competenceDate",
    "Valor (R$)": "paid",
    "Categoria 1": "categoriesRatio.category",
    "Descrição": "description",
    "Nome do fornecedor/cliente": "financialEvent.negotiator.name",
    "Data do último pagamento": "lastAcquittanceDate"
}

colunas_renomeadas = {}
for col_antiga, col_nova in colunas_renomear.items():
    if col_antiga in df_consolidado.columns:
        colunas_renomeadas[col_antiga] = col_nova
        print(f"  ✅ '{col_antiga}' → '{col_nova}'")
    else:
        print(f"  ⚠️ Coluna '{col_antiga}' não encontrada")

df_consolidado.rename(columns=colunas_renomeadas, inplace=True)

# ===================== Converter todos os valores para string =====================
print(f"\n🔄 Convertendo todos os valores para string para evitar auto-formatação...")

for col in df_consolidado.columns:
    df_consolidado[col] = df_consolidado[col].astype(str)
    print(f"  ✅ Coluna '{col}' convertida para string")

# ===================== Buscar ID da planilha no Google Drive =====================
folder_id = "18gmNsGXyzgRlrN3iC7go9oHqp0Q3kzOx"
sheet_name = "Financeiro_contas_a_receber_Quintoserv"

query = f"name='{sheet_name}' and mimeType='application/vnd.google-apps.spreadsheet' and '{folder_id}' in parents and trashed=false"
results = drive_service.files().list(q=query, spaces='drive', fields="files(id, name)").execute()
files = results.get("files", [])

if not files:
    raise Exception(f"Planilha '{sheet_name}' não encontrada na pasta do Drive.")

spreadsheet_id = files[0]['id']

# ===================== Limpar conteúdo anterior da planilha =====================
print(f"\n🧹 Limpando planilha '{sheet_name}'...")
sheets_service.spreadsheets().values().clear(
    spreadsheetId=spreadsheet_id,
    range="A:BA"
).execute()

# ===================== Atualizar dados na planilha com RAW =====================
print(f"📤 Atualizando planilha com {len(df_consolidado)} registros...")
values = [df_consolidado.columns.tolist()] + df_consolidado.fillna("").values.tolist()
sheets_service.spreadsheets().values().update(
    spreadsheetId=spreadsheet_id,
    range="A1",
    valueInputOption="RAW",  # ⬅️ MUDANÇA AQUI: Evita interpretação automática
    body={"values": values}
).execute()

print(f"\n✅ Planilha Google '{sheet_name}' atualizada com sucesso!")
print(f"📊 Total de registros: {len(df_consolidado)}")
print(f"📊 Registros por status (após ajustes):")
for status in ['ACQUITTED', 'PARTIAL', 'PENDING', 'LOST', 'RENEGOTIATED', 'OVERDUE']:
    count = len(df_consolidado[df_consolidado['status'] == status])
    if count > 0:
        print(f"  - {status}: {count} registros")
