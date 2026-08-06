import os
import json
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
    "Financeiro_contas_a_receber_Bluefields": "1IkG2bG2qwfUPIRwE-igQ-gbsH5B3vmlH_ButiOoorAM",
    "Financeiro_contas_a_pagar_Bluefields": "1As4IarqpWofUxl6g4X0TRuBMIgP-uJEFkWDIqqFcZBY",
    "Financeiro_Completo_Bluefields": "1sBKeD9Bgwy59xAJzetF1gVDShrnCocQnuYB2CRtutPk"
}

# === Função para abrir e ler planilha por ID ===
def ler_planilha_por_id(nome_arquivo):
    planilha = client.open_by_key(planilhas_ids[nome_arquivo])
    aba = planilha.sheet1
    df = get_as_dataframe(aba).dropna(how="all")
    return df

# Lê os dados das planilhas principais
print("📥 Lendo planilhas de contas a receber e contas a pagar...")
df_receber = ler_planilha_por_id("Financeiro_contas_a_receber_Bluefields")
df_pagar = ler_planilha_por_id("Financeiro_contas_a_pagar_Bluefields")

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
planilha_saida = client.open_by_key(planilhas_ids["Financeiro_Completo_Bluefields"])
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
    
    # Adiciona índice único para facilitar o merge
    df_completo_indexed = df_completo.reset_index(drop=False)
    df_completo_indexed = df_completo_indexed.rename(columns={'index': 'row_id'})
    
    # Atualiza colunas_id para incluir row_id
    colunas_id_merge = ['row_id'] + colunas_id
    
    # Melt dos centros de custo
    df_melted_cc = pd.melt(
        df_completo_indexed,
        id_vars=colunas_id_merge,
        value_vars=colunas_centro_custo,
        var_name='Centro_de_Custo_Temp',
        value_name='Centro_de_Custo_Unificado'
    )
    
    # Melt dos valores
    df_melted_valor = pd.melt(
        df_completo_indexed,
        id_vars=colunas_id_merge,
        value_vars=colunas_valor,
        var_name='Valor_Temp',
        value_name='paid_new'
    )
    
    # Extrai o número do centro de custo de cada coluna para fazer o match
    df_melted_cc['num'] = df_melted_cc['Centro_de_Custo_Temp'].str.extract(r'(\d+)$').astype(int)
    df_melted_valor['num'] = df_melted_valor['Valor_Temp'].str.extract(r'(\d+)$').astype(int)
    
    # Junta os dois dataframes pelo row_id e número do centro de custo
    df_final = df_melted_cc.merge(
        df_melted_valor[['row_id', 'num', 'paid_new']],
        on=['row_id', 'num'],
        how='left'
    )
    
    # Remove colunas temporárias
    df_final = df_final.drop(columns=['Centro_de_Custo_Temp', 'row_id', 'num'])
    
    # Converte valores negativos em positivos
    if 'paid_new' in df_final.columns:
        df_final['paid_new'] = pd.to_numeric(df_final['paid_new'], errors='coerce')
        df_final['paid_new'] = df_final['paid_new'].abs()
        print("  ✅ Valores negativos convertidos para positivos")

    # Remove linhas com NaN na coluna Centro_de_Custo_Unificado
    df_final = df_final.dropna(subset=['Centro_de_Custo_Unificado'])

    # Remove strings vazias e 'nan' como string
    df_final = df_final[
        (df_final['Centro_de_Custo_Unificado'].astype(str).str.strip() != '') & 
        (df_final['Centro_de_Custo_Unificado'].astype(str).str.strip() != 'nan')
    ]

    print(f"  ✅ Linhas com NaN removidas. Total de registros após limpeza: {len(df_final)}")
    
    # Cria nova aba ou atualiza aba existente
    try:
        aba_pivotada = planilha_saida.worksheet("Dados_Pivotados")
        aba_pivotada.clear()
    except:
        aba_pivotada = planilha_saida.add_worksheet(title="Dados_Pivotados", rows=len(df_final)+1, cols=len(df_final.columns))
    
    set_with_dataframe(aba_pivotada, df_final)
    print("✅ Planilha pivotada criada/atualizada com sucesso!")
    print(f"📋 Total de colunas na planilha pivotada: {len(df_final.columns)}")
else:
    print("⚠️ Nenhuma coluna de centro de custo encontrada para pivotagem")

print("\n🎉 Processamento concluído com sucesso!")
