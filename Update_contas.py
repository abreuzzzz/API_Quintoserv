import os
import time
import glob
import subprocess

# Caminho onde estão os scripts
caminho_scripts = "./"  # ajuste aqui se estiverem em outro diretório

# Lista todos os arquivos com o padrão especificado
arquivos = glob.glob(os.path.join(caminho_scripts, "A*.py"))

# Ordena os arquivos em ordem alfabética (funciona para nomes padronizados como os seus)
arquivos.sort()

# Executa os scripts um por um
for arquivo in arquivos:
    print(f"\nExecutando: {arquivo}")
    try:
        resultado = subprocess.run(["python", arquivo], check=True)
        print(f"✔️ Finalizado com sucesso: {arquivo}")
    except subprocess.CalledProcessError as e:
        print(f"❌ Erro ao executar {arquivo}: {e}")
    time.sleep(10)

print("\nTodos os scripts foram processados.")
