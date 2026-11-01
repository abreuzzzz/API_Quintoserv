import os
import json
import re
import gspread
import pandas as pd
from gspread_dataframe import get_as_dataframe, set_with_dataframe
from oauth2client.service_account import ServiceAccountCredentials

# 🔐 Lê o segredo e salva como credentials.json
gdrive_credentials = os.getenv("GDRIVE_SERVICE_ACCOUNT")
with open("credentials.json", "w") as f:
    json.dump(json.loads(gdrive_credentials), f)

# 📌 Autenticação com Google
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)

# === IDs das planilhas ===
planilhas_ids = {
    "Financeiro_contas_a_receber_Quintoserv": "189qSmHHsfd_nnTtbwfjGbRQmX-6fa2SWcplXj6B88Fk",
    "Financeiro_contas_a_pagar_Quintoserv": "1HzVPftW5N7WbDp1jBTd-NUVhRYVws_bpGKhm2eDl1ac",
    "Financeiro_Completo_Quintoserv": "1wr60d1I6tp-YlZYg_jkTdOypN8Y7W83HaMuSkE-3JXU"
}

# === Conversão de valores em moeda em BRL ===
def parse_valor_brl(valor):
    if pd.isna(valor):
        return pd.NA

    if isinstance(valor, str):
        valor = valor.strip()
        if valor in ('', 'nan', 'NaN', 'None', '-'):
            return pd.NA

        valor = valor.replace(' ', '')
        if valor.startswith('(') and valor.endswith(')'):
            valor = '-' + valor[1:-1]

        if ',' in valor and '.' in valor:
            if valor.rfind(',') > valor.rfind('.'):
                valor = valor.replace('.', '').replace(',', '.')
            else:
                valor = valor.replace(',', '')
        elif ',' in valor:
            valor = valor.replace(',', '.')

    try:
        return float(valor)
    except (TypeError, ValueError):
        return pd.NA

# === Função para abrir e ler planilha por ID ===
def ler_planilha_por_id(nome_arquivo):
    planilha = client.open_by_key(planilhas_ids[nome_arquivo])
    aba = planilha.sheet1
    df = get_as_dataframe(aba).dropna(how="all")
    return df

# Lê os dados das planilhas principais
print("📥 Lendo planilhas de contas a receber e contas a pagar...")
df_receber = ler_planilha_por_id("Financeiro_contas_a_receber_Martins")
df_pagar = ler_planilha_por_id("Financeiro_contas_a_pagar_Martins")

# Adiciona a coluna tipo
df_receber["tipo"] = "Receita"
df_pagar["tipo"] = "Despesa"

# Junta os dois dataframes
print("🔗 Consolidando dados de receitas e despesas...")
df_completo = pd.concat([df_receber, df_pagar], ignore_index=True)

# === CONVERSÃO DAS DATAS PARA FORMATO YYYY-MM-DD ===
campos_data = ['lastAcquittanceDate', 'financialEvent.competenceDate', 'dueDate']

print("📅 Convertendo campos de data para formato YYYY-MM-DD...")
for campo in campos_data:
    if campo in df_completo.columns:
        df_completo[campo] = pd.to_datetime(
            df_completo[campo], 
            format='mixed',
            dayfirst=True,
            errors='coerce'
        )
        df_completo[campo] = df_completo[campo].dt.strftime('%Y-%m-%d')
        df_completo[campo] = df_completo[campo].replace('NaT', '')

# Corrige valores da coluna categoriesRatio.value com base na condição
if 'categoriesRatio.value' in df_completo.columns and 'paid' in df_completo.columns:
    print("💰 Corrigindo valores de categoriesRatio.value...")
    df_completo['categoriesRatio.value'] = df_completo.apply(
        lambda row: row['paid'] if pd.notna(row['categoriesRatio.value']) and pd.notna(row['paid']) and row['categoriesRatio.value'] > row['paid'] else row['categoriesRatio.value'],
        axis=1
    )

# === TRATAMENTO PARA REGISTROS SEM CENTRO DE CUSTO ===
print("\n🔍 Verificando registros sem centro de custo...")

# Identifica todas as colunas de Centro de Custo e seus respectivos valores
colunas_centro_custo = [col for col in df_completo.columns if col.startswith("Centro de Custo ") and not col.startswith("Valor no Centro de Custo ")]
colunas_valor_cc = [col for col in df_completo.columns if col.startswith("Valor no Centro de Custo ")]

print(f"  Encontradas {len(colunas_centro_custo)} colunas de centro de custo para processar")

if len(colunas_centro_custo) > 0 and 'paid' in df_completo.columns:
    total_registros_com_valor = 0
    total_apenas_cc_preenchido = 0
    
    # Cria uma máscara para rastrear linhas já processadas (apenas para o cenário "centro + valor")
    linhas_com_valor_preenchido = pd.Series([False] * len(df_completo), index=df_completo.index)
    
    # Itera sobre cada par de colunas Centro de Custo / Valor
    for i, col_centro in enumerate(colunas_centro_custo, start=1):
        # Encontra a coluna de valor correspondente
        col_valor = f"Valor no Centro de Custo {i}"
        
        # Verifica se a coluna de valor existe
        if col_valor not in df_completo.columns:
            print(f"  ⚠️ Coluna '{col_valor}' não encontrada, pulando...")
            continue
        
        # Normaliza a coluna de centro de custo
        df_completo[col_centro] = df_completo[col_centro].astype(str).str.strip()
        
        # Máscara para centro de custo vazio
        mask_centro_vazio = (df_completo[col_centro].isna()) | (df_completo[col_centro] == '') | (df_completo[col_centro] == 'nan')
        
        # Máscara para valor vazio
        mask_valor_vazio = (df_completo[col_valor].isna()) | (df_completo[col_valor] == '') | (df_completo[col_valor] == 0)
        
        # Caso 1: Centro vazio E valor vazio - preenche ambos (SOMENTE NO CENTRO 1 E SE NÃO FOI PREENCHIDO ANTES)
        if i == 1:  # Apenas para Centro de Custo 1
            mask_ambos_vazios = mask_centro_vazio & mask_valor_vazio & (~linhas_com_valor_preenchido)
            registros_ambos = mask_ambos_vazios.sum()
            
            if registros_ambos > 0:
                df_completo.loc[mask_ambos_vazios, col_centro] = 'Sem Centro de Custo'
                df_completo.loc[mask_ambos_vazios, col_valor] = df_completo.loc[mask_ambos_vazios, 'paid']
                total_registros_com_valor += registros_ambos
                
                # Marca essas linhas como já tendo recebido valor
                linhas_com_valor_preenchido = linhas_com_valor_preenchido | mask_ambos_vazios
                
                print(f"  ✅ '{col_centro}': {registros_ambos} registros preenchidos (centro + valor copiado de 'paid')")
        
        # Caso 2: Centro vazio MAS valor existe - preenche apenas o centro
        # Para Centro 1: aplica normalmente
        # Para Centros 2+: SÓ aplica se o valor NÃO estiver vazio (ou seja, pula se ambos estiverem vazios)
        mask_so_centro_vazio = mask_centro_vazio & (~mask_valor_vazio)
        registros_so_centro = mask_so_centro_vazio.sum()
        
        if registros_so_centro > 0:
            df_completo.loc[mask_so_centro_vazio, col_centro] = 'Sem Centro de Custo'
            total_apenas_cc_preenchido += registros_so_centro
            print(f"  ✅ '{col_centro}': {registros_so_centro} registros preenchidos (apenas centro, valor mantido)")
    
    # Resumo final
    print(f"\n  📊 Resumo do tratamento:")
    print(f"    Registros com centro + valor preenchidos (apenas Centro 1): {total_registros_com_valor}")
    print(f"    Registros com apenas centro preenchido (todos os centros): {total_apenas_cc_preenchido}")
    
else:
    print("  ⚠️ Colunas necessárias não encontradas para tratamento de centro de custo")






# Estatísticas finais
print(f"\n📊 Resumo dos dados processados:")
print(f"  Total de registros: {len(df_completo)}")
if 'tipo' in df_completo.columns:
    print(f"  Receitas: {len(df_completo[df_completo['tipo'] == 'Receita'])}")
    print(f"  Despesas: {len(df_completo[df_completo['tipo'] == 'Despesa'])}")
if 'Centro de Custo 1' in df_completo.columns:
    centros_custo = df_completo['Centro de Custo 1'].nunique()
    print(f"  Centros de custo únicos: {centros_custo}")

# 📄 Abrir a planilha de saída e escrever UMA ÚNICA VEZ
print("\n📤 Atualizando planilha consolidada...")
planilha_saida = client.open_by_key(planilhas_ids["Financeiro_Completo_Martins"])
aba_saida = planilha_saida.sheet1

# Limpa a aba e sobrescreve
aba_saida.clear()
set_with_dataframe(aba_saida, df_completo)

print("✅ Planilha consolidada atualizada com sucesso!")
print(f"📋 Total de colunas exportadas: {len(df_completo.columns)}")

# === NOVA ETAPA: PIVOTAGEM DOS CENTROS DE CUSTO ===
print("\n🔄 Iniciando pivotagem dos centros de custo...")

# Identifica as colunas de centro de custo e valor
colunas_centro_custo = [col for col in df_completo.columns if col.startswith("Centro de Custo ") and not col.startswith("Valor no Centro de Custo ")]
colunas_valor = [col for col in df_completo.columns if col.startswith("Valor no Centro de Custo ")]

print(f"  Encontradas {len(colunas_centro_custo)} colunas de centro de custo")
print(f"  Encontradas {len(colunas_valor)} colunas de valor")

if len(colunas_centro_custo) > 0 and len(colunas_valor) > 0:
    # Cria lista com todas as outras colunas que não são centro de custo
    colunas_id = [col for col in df_completo.columns if col not in colunas_centro_custo + colunas_valor]

    # Adiciona índice único para facilitar a montagem por linha
    df_completo_indexed = df_completo.reset_index(drop=False)
    df_completo_indexed = df_completo_indexed.rename(columns={'index': 'row_id'})

    def localizar_coluna_valor(centro_col):
        sufixo = centro_col.replace('Centro de Custo ', '', 1).strip()

        # 1) padrão novo: nome exato do valor, incluindo decimais como 1.1 / 1.2
        candidatos = [
            f'Valor no Centro de Custo {sufixo}',
            f'Valor no Centro de Custo {sufixo.replace(".", ".")}'
        ]

        for nome in candidatos:
            if nome in df_completo_indexed.columns:
                return nome

        # 2) compatibilidade com nomes que terminam no mesmo sufixo
        for nome in df_completo_indexed.columns:
            if nome.startswith('Valor no Centro de Custo ') and nome.endswith(sufixo):
                return nome

        # 3) fallback da forma antiga: ordem sequencial (1, 2, 3...)
        if centro_col in colunas_centro_custo:
            indice_legacy = colunas_centro_custo.index(centro_col) + 1
            nome_legacy = f'Valor no Centro de Custo {indice_legacy}'
            if nome_legacy in df_completo_indexed.columns:
                return nome_legacy

        return None

    # Monta os pares (Centro de Custo, Valor) sem repetir toda a linha da transação
    registros_pivotados = []
    for col_centro in colunas_centro_custo:
        col_valor = localizar_coluna_valor(col_centro)
        if col_valor is None:
            continue

        subset = df_completo_indexed[[*colunas_id, 'row_id', col_centro, col_valor]].copy()
        subset = subset.rename(columns={col_centro: 'Centro_de_Custo_Unificado', col_valor: 'paid_new'})

        subset = subset[
            subset['Centro_de_Custo_Unificado'].astype(str).str.strip().ne('') &
            subset['Centro_de_Custo_Unificado'].astype(str).str.strip().ne('nan')
        ]

        subset['paid_new'] = subset['paid_new'].apply(parse_valor_brl)
        subset = subset.dropna(subset=['paid_new'])
        subset['paid_new'] = subset['paid_new'].abs()

        registros_pivotados.append(subset)

    if registros_pivotados:
        df_final = pd.concat(registros_pivotados, ignore_index=True)
        df_final = df_final.drop_duplicates(subset=['row_id', 'Centro_de_Custo_Unificado', 'paid_new'], keep='first')
        df_final = df_final.drop(columns=['row_id'])
    else:
        df_final = pd.DataFrame()

    print(f"  ✅ Registros pivotados montados. Total final: {len(df_final)}")

    # Remove linhas com NaN na coluna Centro_de_Custo_Unificado
    if not df_final.empty:
        df_final = df_final.dropna(subset=['Centro_de_Custo_Unificado'])
        df_final = df_final[
            (df_final['Centro_de_Custo_Unificado'].astype(str).str.strip() != '') &
            (df_final['Centro_de_Custo_Unificado'].astype(str).str.strip() != 'nan')
        ]

    print(f"  ✅ Linhas após limpeza: {len(df_final)}")

    # Cria nova aba ou atualiza aba existente
    try:
        aba_pivotada = planilha_saida.worksheet("Dados_Pivotados")
        aba_pivotada.clear()
    except:
        aba_pivotada = planilha_saida.add_worksheet(title="Dados_Pivotados", rows=max(1, len(df_final)+1), cols=max(1, len(df_final.columns) if not df_final.empty else 1))

    if not df_final.empty:
        set_with_dataframe(aba_pivotada, df_final)
        print("✅ Planilha pivotada criada/atualizada com sucesso!")
        print(f"📋 Total de colunas na planilha pivotada: {len(df_final.columns)}")
    else:
        print("⚠️ Nenhum registro válido para gravar na aba Dados_Pivotados")
else:
    print("⚠️ Nenhuma coluna de centro de custo encontrada para pivotagem")

print("\n🎉 Processamento concluído com sucesso!")
