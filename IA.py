import json
import pandas as pd
from openai import OpenAI
import os
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from google.oauth2.service_account import Credentials

deepseek_api_key = os.getenv("DEEPSEEK_API_KEY")
client = OpenAI(api_key=deepseek_api_key, base_url="https://api.deepseek.com")

# URL da planilha Google Sheets exportada como CSV
sheet_id = "1sBKeD9Bgwy59xAJzetF1gVDShrnCocQnuYB2CRtutPk"
gid = "1038646143"

sheet_csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
SHEET_ID2 = "1L6eJ5ux2OkdUTB8CCYUkKGxn76oVx92zqDRLcf-2wCM"

# Ler a planilha
df = pd.read_csv(sheet_csv_url, low_memory=False)

print("=== COLUNAS DISPONÍVEIS ===")
print(df.columns.tolist())
print("\n=== PRIMEIRAS LINHAS ===")
print(df.head())

# Limpar valores monetários
def limpar_valores(col):
    return (
        col.astype(str)
           .str.replace(r"[^\d,.-]", "", regex=True)
           .str.replace(".", "", regex=False)
           .str.replace(",", ".", regex=False)
           .pipe(pd.to_numeric, errors="coerce")
    )

df['paid_new'] = limpar_valores(df['paid_new'])

# Converter coluna de data
def parse_data_segura(coluna):
    datas = pd.to_datetime(
        coluna.apply(lambda x: '-'.join(x.split('-')[:3]) if isinstance(x, str) and '-' in x else None),
        format='%Y-%m-%d',
        errors='coerce'
    )
    return datas

df['lastAcquittanceDate'] = parse_data_segura(df['lastAcquittanceDate'])
df['dueDate'] = parse_data_segura(df['dueDate'])

# Filtrar apenas registros do ano corrente
ano_corrente = datetime.today().year
df = df[df['lastAcquittanceDate'].dt.year == ano_corrente]

# Criar colunas auxiliares
df['AnoMes'] = df['lastAcquittanceDate'].dt.to_period('M')
df['Trimestre'] = df['lastAcquittanceDate'].dt.to_period('Q')
df['AnoMes_Caixa'] = df['lastAcquittanceDate'].dt.to_period('M')
df['Trimestre_Caixa'] = df['lastAcquittanceDate'].dt.to_period('Q')

# Normalizar valores da coluna 'tipo'
df['tipo'] = df['tipo'].str.strip().str.capitalize()

# ================= CÁLCULOS AGREGADOS ===================

# Resumo trimestral
resumo_trimestral = df.groupby(['Trimestre', 'tipo'])['paid_new'].sum().unstack(fill_value=0)

# Variação mensal por categoria (só últimos 6 meses)
resumo_mensal_categoria = df.groupby(['AnoMes', 'categoriesRatio.category'])['paid_new'].sum().unstack(fill_value=0)
variacao_mensal_pct = resumo_mensal_categoria.pct_change().fillna(0)

# Pegar apenas categorias com alta > 30% e últimos 3 meses
categorias_com_alta = {}
for mes in variacao_mensal_pct.tail(3).index:
    altas = variacao_mensal_pct.loc[mes][variacao_mensal_pct.loc[mes] > 0.3].to_dict()
    if altas:
        categorias_com_alta[str(mes)] = altas

# Valores totais
total_recebido = df[df['tipo'] == 'Receita']['paid_new'].sum()
total_pago = df[df['tipo'] == 'Despesa']['paid_new'].sum()
total_pendente_despesa = df[
    (df['tipo'] == 'Despesa') & (df['status'] == 'OVERDUE')
]['paid_new'].sum()
total_pendente_receita = df[
    (df['tipo'] == 'Receita') & (df['status'] == 'OVERDUE')
]['paid_new'].sum()
saldo_liquido = total_recebido - total_pago

# Top 3 categorias
top_categorias = df['categoriesRatio.category'].value_counts().head(3).to_dict()

# Filtrar transações realizadas
hoje = pd.to_datetime(datetime.today().date())
df_realizadas = df[df['lastAcquittanceDate'] <= hoje].copy()

df_realizadas['valor_ajustado'] = df_realizadas.apply(
    lambda row: abs(row['paid_new']) if row['tipo'] == 'Receita' else -abs(row['paid_new']),
    axis=1
)

# Fluxo de Caixa (últimos 6 meses)
fluxo_caixa = df_realizadas.groupby('AnoMes_Caixa')['valor_ajustado'].sum().reset_index()
fluxo_caixa['saldo_acumulado'] = fluxo_caixa['valor_ajustado'].cumsum()
fluxo_caixa_ultimos = fluxo_caixa.tail(6)

# Receitas e despesas por mês
df_receitas = df_realizadas[df_realizadas['tipo'] == 'Receita']
df_despesas = df_realizadas[df_realizadas['tipo'] == 'Despesa']

receitas_mensais = df_receitas.groupby('AnoMes')['paid_new'].sum().reset_index()
receitas_mensais.columns = ['AnoMes', 'paid_receita']

despesas_mensais = df_despesas.groupby('AnoMes')['paid_new'].sum().reset_index()
despesas_mensais.columns = ['AnoMes', 'paid_despesa']

# Rentabilidade (últimos 6 meses)
rentabilidade = pd.merge(
    receitas_mensais,
    despesas_mensais,
    on='AnoMes',
    how='outer'
).fillna(0)

rentabilidade['lucro'] = rentabilidade['paid_receita'] - rentabilidade['paid_despesa']
rentabilidade['margem_lucro'] = rentabilidade.apply(
    lambda row: (row['lucro'] / row['paid_receita']) if row['paid_receita'] > 0 else 0,
    axis=1
)
rentabilidade_ultimos = rentabilidade.tail(6)

# Inadimplência
df_pendentes = df[(df['paid_new'] > 0) & (df['dueDate'] <= hoje) & (df['status'] == 'OVERDUE')]
total_vencido = df_pendentes[df_pendentes['tipo'] == 'Receita']['paid_new'].sum()
inadimplencia = total_vencido / total_recebido if total_recebido > 0 else 0

# ================= MONTAR JSON COMPACTO ===================

# Simplificar estrutura trimestral
resumo_trimestral_simples = {}
for trimestre in resumo_trimestral.index:
    resumo_trimestral_simples[str(trimestre)] = {
        'Receita': float(resumo_trimestral.loc[trimestre, 'Receita'] if 'Receita' in resumo_trimestral.columns else 0),
        'Despesa': float(resumo_trimestral.loc[trimestre, 'Despesa'] if 'Despesa' in resumo_trimestral.columns else 0)
    }

resumo = {
    "visao_geral": {
        "total_recebido": float(total_recebido),
        "total_pago": float(total_pago),
        "receita_pendente": float(total_pendente_receita),
        "despesa_pendente": float(total_pendente_despesa),
        "saldo_liquido": float(saldo_liquido),
        "inadimplencia_pct": float(inadimplencia),
    },
    "top_3_categorias": {k: int(v) for k, v in list(top_categorias.items())[:3]},
    "resumo_trimestral": resumo_trimestral_simples,
    "categorias_alta_recente": categorias_com_alta,
    "fluxo_caixa_ultimos_6m": [
        {
            "mes": str(row['AnoMes_Caixa']),
            "valor": float(row['valor_ajustado']),
            "saldo_acum": float(row['saldo_acumulado'])
        }
        for _, row in fluxo_caixa_ultimos.iterrows()
    ],
    "rentabilidade_ultimos_6m": [
        {
            "mes": str(row['AnoMes']),
            "lucro": float(row['lucro']),
            "margem_lucro_pct": float(row['margem_lucro'])
        }
        for _, row in rentabilidade_ultimos.iterrows()
    ],
}

# ================= PROMPT OTIMIZADO ===================

prompt = f"""Analise os dados financeiros JSON abaixo e gere um resumo executivo com:

1. Insights sobre saúde financeira e tendências
2. Sinais de alerta (pendências, desequilíbrios)
3. Oportunidades de otimização (custos, previsibilidade)
4. Recomendações práticas por trimestre e categoria

DADOS:
{json.dumps(resumo, ensure_ascii=False, indent=2)}

Seja objetivo e direto."""

# ================= CHAMAR API DEEPSEEK ===================

response = client.chat.completions.create(
    model="deepseek-chat",
    messages=[
        {"role": "system", "content": "Você é um analista financeiro sênior. Seja objetivo e direto."},
        {"role": "user", "content": prompt}
    ],
    max_tokens=800,
    temperature=0.7
)

print("\n=== INSIGHTS GERADOS ===")
print(response.choices[0].message.content)

# ================= SALVAR NO GOOGLE SHEETS ===================

json_secret = os.getenv("GDRIVE_SERVICE_ACCOUNT")
creds_dict = json.loads(json_secret)
creds = Credentials.from_service_account_info(
    creds_dict, 
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)

gc = gspread.authorize(creds)
spreadsheet = gc.open_by_key(SHEET_ID2)
worksheet = spreadsheet.get_worksheet(0)

worksheet.clear()

conteudo_ia = response.choices[0].message.content

# Processar blocos
blocos = conteudo_ia.split("####")
dados = []

for bloco in blocos:
    bloco = bloco.strip()
    if not bloco:
        continue
    if bloco.startswith("**"):
        titulo = bloco.split("**")[1]
        resultado = bloco.split("**", 2)[-1].strip()
        dados.append([titulo, resultado])

# Se não houver blocos formatados, salvar texto completo
if not dados:
    dados = [["Análise Financeira", conteudo_ia]]

worksheet.update(dados, "A1")

print("\n=== ANÁLISE SALVA NA PLANILHA COM SUCESSO ===")
