
import streamlit as st
import pandas as pd
import re
import io
import json
import base64
import requests
import altair as alt
from pathlib import Path
from datetime import date, datetime, timedelta

st.set_page_config(page_title="Gestão Comercial", page_icon="📈", layout="wide")



DATABASE_PATH = Path("database.json")
ARQUIVO_INICIAL = Path("celso comercial.xlsx")

def github_config():
    try:
        token = str(st.secrets.get("GITHUB_TOKEN", "")).strip()
        repo = str(st.secrets.get("GITHUB_REPO", "")).strip()
        branch = str(st.secrets.get("GITHUB_DATA_BRANCH", "database")).strip() or "database"
        db_path = str(st.secrets.get("GITHUB_DB_PATH", "database.json")).strip() or "database.json"
    except Exception:
        token = repo = ""
        branch = "database"
        db_path = "database.json"
    return token, repo, branch, db_path

def github_ativo():
    token, repo, _, _ = github_config()
    return bool(token and repo)

def github_headers():
    token, _, _, _ = github_config()
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def github_file_info(path_repo=None):
    if not github_ativo():
        return None
    _, repo, branch, db_path = github_config()
    path_repo = path_repo or db_path
    r = requests.get(
        f"https://api.github.com/repos/{repo}/contents/{path_repo}",
        headers=github_headers(),
        params={"ref": branch},
        timeout=30,
    )
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise RuntimeError(f"GitHub respondeu {r.status_code} ao consultar a base.")
    return r.json()

def github_baixar_database():
    info = github_file_info()
    if not info:
        return False

    conteudo = info.get("content")
    if not conteudo:
        raise RuntimeError("GitHub não retornou o conteúdo do database.json.")

    dados_bytes = base64.b64decode(conteudo)
    try:
        dados = json.loads(dados_bytes.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("O database.json salvo no GitHub está inválido.") from exc

    dados.setdefault("metadata", {})
    dados.setdefault("empresas", [])
    dados.setdefault("contatos", [])

    tmp = DATABASE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    tmp.replace(DATABASE_PATH)
    st.session_state["_database_remota_carregada"] = True
    return True

def github_criar_backup_do_atual():
    """Cria um snapshot diário da base anterior antes da primeira alteração do dia."""
    if not github_ativo():
        return

    _, repo, branch, db_path = github_config()
    atual = github_file_info(db_path)
    if not atual:
        return

    backup_path = f"backups/database_{date.today().isoformat()}.json"

    # Se já existe backup de hoje, não recria.
    r_check = requests.get(
        f"https://api.github.com/repos/{repo}/contents/{backup_path}",
        headers=github_headers(),
        params={"ref": branch},
        timeout=30,
    )
    if r_check.status_code == 200:
        return

    payload = {
        "message": f"Backup database {date.today().strftime('%d/%m/%Y')}",
        "content": atual.get("content", ""),
        "branch": branch,
    }
    requests.put(
        f"https://api.github.com/repos/{repo}/contents/{backup_path}",
        headers=github_headers(),
        json=payload,
        timeout=45,
    )

def github_salvar_database(dados):
    """Salva a base oficial no GitHub. Falha explícita se não conseguir."""
    if not github_ativo():
        raise RuntimeError(
            "A base persistente do GitHub não está configurada. "
            "Não é seguro salvar dados no Streamlit sem essa conexão."
        )

    _, repo, branch, db_path = github_config()

    # Backup diário antes da primeira alteração do dia.
    try:
        github_criar_backup_do_atual()
    except Exception:
        # O backup extra não pode impedir o salvamento principal.
        pass

    info = github_file_info(db_path)
    conteudo = json.dumps(dados, ensure_ascii=False, indent=2)
    payload = {
        "message": f"Atualiza database.json - {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}",
        "content": base64.b64encode(conteudo.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }

    if info:
        payload["sha"] = info["sha"]

    r = requests.put(
        f"https://api.github.com/repos/{repo}/contents/{db_path}",
        headers=github_headers(),
        json=payload,
        timeout=60,
    )

    if r.status_code not in (200, 201):
        detalhe = ""
        try:
            detalhe = r.json().get("message", "")
        except Exception:
            pass
        raise RuntimeError(
            f"Não foi possível salvar a base no GitHub ({r.status_code}). {detalhe}"
        )

def salvar_database(dados):
    """
    A base oficial é o database.json da branch 'database'.
    Primeiro grava no GitHub; só depois atualiza a cópia local.
    """
    dados.setdefault("metadata", {})
    dados.setdefault("empresas", [])
    dados.setdefault("contatos", [])
    dados["metadata"]["ultima_atualizacao"] = datetime.now().isoformat(timespec="seconds")
    dados["metadata"]["ultimo_usuario"] = st.session_state.get("usuario_logado", "")

    github_salvar_database(dados)

    tmp = DATABASE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    tmp.replace(DATABASE_PATH)
    st.session_state["_database_remota_carregada"] = True

def carregar_database(forcar_github=False):
    """
    Ao iniciar uma sessão, baixa a base oficial do GitHub.
    Em leituras seguintes usa a cópia local da mesma sessão.
    """
    if github_ativo() and (
        forcar_github or not st.session_state.get("_database_remota_carregada", False)
    ):
        existe_remota = github_baixar_database()
        if not existe_remota:
            # Primeira inicialização: publica a base local atual na branch database.
            if not DATABASE_PATH.exists():
                dados_seed = {
                    "metadata": {
                        "app": "Gestão Comercial",
                        "database_version": 1,
                        "format": "json",
                    },
                    "empresas": [],
                    "contatos": [],
                }
                with open(DATABASE_PATH, "w", encoding="utf-8") as f:
                    json.dump(dados_seed, f, ensure_ascii=False, indent=2)

            with open(DATABASE_PATH, "r", encoding="utf-8") as f:
                dados_seed = json.load(f)

            github_salvar_database(dados_seed)
            st.session_state["_database_remota_carregada"] = True

    if not DATABASE_PATH.exists():
        raise RuntimeError(
            "database.json não encontrado. Não continue utilizando o app até restaurar a base."
        )

    try:
        with open(DATABASE_PATH, "r", encoding="utf-8") as f:
            dados = json.load(f)
    except Exception as exc:
        raise RuntimeError(
            "Não foi possível abrir database.json. A base pode estar corrompida."
        ) from exc

    dados.setdefault("metadata", {})
    dados.setdefault("empresas", [])
    dados.setdefault("contatos", [])
    return dados

def proximo_id(lista):
    if not lista:
        return 1
    return max(int(item.get("id", 0) or 0) for item in lista) + 1

def proxima_seq_global():
    dados = carregar_database()
    seqs = [
        int(c.get("seq_global") or 0)
        for c in dados["contatos"]
        if c.get("seq_global") is not None
    ]
    return (max(seqs) if seqs else 0) + 1

def seq_global_atual():
    dados = carregar_database()
    seqs = [
        int(c.get("seq_global") or 0)
        for c in dados["contatos"]
        if c.get("seq_global") is not None
    ]
    return max(seqs) if seqs else 0

# -----------------------------
# REGRAS COMERCIAIS
# -----------------------------
RESULTADOS = [
    "NÃO CONSEGUI CONTATO",
    "CLIENTE RESPONDEU",
    "AGUARDANDO CLIENTE",
    "RETORNAR EM OUTRA DATA",
    "REUNIÃO AGENDADA",
    "SOLICITOU PROPOSTA",
    "PROPOSTA ENVIADA",
    "EM NEGOCIAÇÃO",
    "FECHADO",
    "SEM INTERESSE",
    "NÃO UTILIZA TRANSPORTE",
    "JÁ UTILIZA AZUL",
    "CONTATO INVÁLIDO",
    "OUTRO",
]

TIPOS_CONTATO = ["LIGAÇÃO", "WHATSAPP", "E-MAIL", "REUNIÃO", "OUTRO"]

ACOES_SUGERIDAS = [
    "LIGAR NOVAMENTE",
    "ENVIAR WHATSAPP",
    "ENVIAR E-MAIL",
    "AGUARDAR RETORNO DO CLIENTE",
    "AGENDAR REUNIÃO",
    "ENVIAR PROPOSTA",
    "FAZER FOLLOW-UP",
    "OUTRO",
]

STATUS_ATIVOS = [
    "1ª TENTATIVA SEM RETORNO",
    "2ª TENTATIVA SEM RETORNO",
    "AGUARDANDO CLIENTE",
    "RETORNO AGENDADO",
    "EM ANDAMENTO",
    "REUNIÃO AGENDADA",
    "PROPOSTA SOLICITADA",
    "PROPOSTA ENVIADA",
    "EM NEGOCIAÇÃO",
    "CONTATO INVÁLIDO",
]

STATUS_ENCERRADOS = [
    "FECHADO / GANHO",
    "SEM INTERESSE",
    "NÃO UTILIZA TRANSPORTE",
    "JÁ UTILIZA AZUL",
]

STATUS_RETORNO_IMPORTADO = {
    "AGUARDANDO CLIENTE",
    "SEM RETORNO",
    "SEM SUCESSO NO CONTATO",
    "TENTATIVA DE CONTATO",
    "1ª TENTATIVA SEM RETORNO",
    "2ª TENTATIVA SEM RETORNO",
    "EM ANDAMENTO",
    "REUNIÃO AGENDADA",
    "PROPOSTA SOLICITADA",
    "PROPOSTA ENVIADA",
    "EM NEGOCIAÇÃO",
    "CONTATO INVÁLIDO",
}


RESULTADOS_AGUARDANDO = {
    "NÃO CONSEGUI CONTATO",
    "CLIENTE RESPONDEU",
    "AGUARDANDO CLIENTE",
    "RETORNAR EM OUTRA DATA",
    "REUNIÃO AGENDADA",
    "SOLICITOU PROPOSTA",
    "PROPOSTA ENVIADA",
    "EM NEGOCIAÇÃO",
    "CONTATO INVÁLIDO",
    "OUTRO",
}

MAPA_STATUS = {
    "CLIENTE RESPONDEU": "EM ANDAMENTO",
    "AGUARDANDO CLIENTE": "AGUARDANDO CLIENTE",
    "RETORNAR EM OUTRA DATA": "RETORNO AGENDADO",
    "REUNIÃO AGENDADA": "REUNIÃO AGENDADA",
    "SOLICITOU PROPOSTA": "PROPOSTA SOLICITADA",
    "PROPOSTA ENVIADA": "PROPOSTA ENVIADA",
    "EM NEGOCIAÇÃO": "EM NEGOCIAÇÃO",
    "FECHADO": "FECHADO / GANHO",
    "SEM INTERESSE": "SEM INTERESSE",
    "NÃO UTILIZA TRANSPORTE": "NÃO UTILIZA TRANSPORTE",
    "JÁ UTILIZA AZUL": "JÁ UTILIZA AZUL",
    "CONTATO INVÁLIDO": "CONTATO INVÁLIDO",
    "OUTRO": "EM ANDAMENTO",
}

# -----------------------------
# BANCO JSON
# -----------------------------
def criar_banco():
    # Mantido apenas por compatibilidade com o restante do app.
    if not DATABASE_PATH.exists():
        salvar_database({
            "metadata": {
                "app": "Gestão Comercial",
                "database_version": 1,
                "format": "json"
            },
            "empresas": [],
            "contatos": []
        })

# -----------------------------
# FORMATAÇÃO / VALIDAÇÃO
# -----------------------------
def somente_digitos(valor):
    return re.sub(r"\D", "", str(valor or ""))

def formatar_documento(valor):
    d = somente_digitos(valor)
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    return str(valor or "").strip()

def documento_valido(valor):
    d = somente_digitos(valor)
    if len(d) not in (11, 14) or not d or d == d[0] * len(d):
        return False
    if len(d) == 11:
        soma = sum(int(d[i]) * (10-i) for i in range(9))
        dv1 = 11 - (soma % 11)
        dv1 = 0 if dv1 >= 10 else dv1
        soma = sum(int(d[i]) * (11-i) for i in range(10))
        dv2 = 11 - (soma % 11)
        dv2 = 0 if dv2 >= 10 else dv2
        return d[-2:] == f"{dv1}{dv2}"

    pesos1 = [5,4,3,2,9,8,7,6,5,4,3,2]
    soma = sum(int(d[i]) * pesos1[i] for i in range(12))
    r = soma % 11
    dv1 = 0 if r < 2 else 11-r
    pesos2 = [6] + pesos1
    soma = sum(int(d[i]) * pesos2[i] for i in range(13))
    r = soma % 11
    dv2 = 0 if r < 2 else 11-r
    return d[-2:] == f"{dv1}{dv2}"

def formatar_telefone(valor):
    d = somente_digitos(valor)
    if len(d) == 11:
        return f"({d[:2]}) {d[2:7]}-{d[7:]}"
    if len(d) == 10:
        return f"({d[:2]}) {d[2:6]}-{d[6:]}"
    return str(valor or "").strip()

def data_br(valor):
    if valor is None or valor == "" or pd.isna(valor):
        return ""
    try:
        return pd.to_datetime(valor).strftime("%d/%m/%Y")
    except Exception:
        return ""

def excel_data(v):
    if pd.isna(v) or v == "":
        return None
    try:
        if isinstance(v, (int, float)):
            dt = pd.Timestamp("1899-12-30") + pd.to_timedelta(float(v), unit="D")
        else:
            dt = pd.to_datetime(v, dayfirst=True, errors="coerce")
        # Datas antigas inválidas não entram na lógica do app.
        if pd.isna(dt) or dt.year < 2000:
            return None
        return dt.date().isoformat()
    except Exception:
        return None

# -----------------------------
# IMPORTAÇÃO INICIAL
# -----------------------------
def importar_planilha_inicial():
    """
    O app final já deve vir com database.json preenchido.
    Esta função apenas mantém compatibilidade e evita reimportação duplicada.
    """
    dados = carregar_database()
    if dados["empresas"]:
        return
    if not ARQUIVO_INICIAL.exists():
        return

    df = pd.read_excel(ARQUIVO_INICIAL, dtype=object)
    df.columns = [str(c).strip().upper() for c in df.columns]
    agora = datetime.now().isoformat(timespec="seconds")

    for _, r in df.iterrows():
        nome = str(r.get("NOME", "") or "").strip()
        if not nome or nome.lower() == "nan":
            continue

        documento = formatar_documento(r.get("CNPJ OU CPF", ""))
        tel1 = formatar_telefone(r.get("TELEFONE 1", ""))
        tel2 = formatar_telefone(r.get("TELEFONE 2", ""))
        tel3 = formatar_telefone(r.get("TELEFONE 3", ""))

        status = str(r.get("STATUS RETORNO DO CLIENTE", "") or "").strip().upper()
        if not status or status == "NAN":
            tentativa = str(r.get("FOI FEITO TENTATIVA DE CONTATO", "") or "").strip().upper()
            status = "TENTATIVA DE CONTATO" if tentativa == "SIM" else "SEM CONTATO"

        obs = str(r.get("OBS.", "") or "").strip()
        if obs.upper() == "NAN":
            obs = ""

        data1 = excel_data(r.get("DATA 1º CONTATO", None))
        empresa_id = proximo_id(dados["empresas"])

        dados["empresas"].append({
            "id": empresa_id,
            "documento": documento,
            "nome": nome,
            "telefone1": tel1,
            "telefone2": tel2,
            "telefone3": tel3,
            "status": status,
            "observacao_atual": obs,
            "data_primeiro_contato": data1,
            "criado_em": agora,
            "origem": "PLANILHA INICIAL",
            "retorno_apos_seq": None,
            "data_agendamento": None,
            "agendamento_pendente": 0,
            "proxima_acao": ""
        })

        if data1:
            dados["contatos"].append({
                "id": proximo_id(dados["contatos"]),
                "empresa_id": empresa_id,
                "data_contato": data1,
                "tipo_contato": "HISTÓRICO IMPORTADO",
                "resultado": "CONTATO REGISTRADO NA PLANILHA",
                "status_novo": status,
                "observacao": obs,
                "proxima_acao": "",
                "data_proxima_acao": None,
                "criado_em": agora,
                "seq_global": None
            })

    salvar_database(dados)

# -----------------------------
# DADOS
# -----------------------------
def carregar_empresas():
    dados = carregar_database()
    df = pd.DataFrame(dados["empresas"])
    if df.empty:
        return pd.DataFrame(columns=[
            "id","documento","nome","telefone1","telefone2","telefone3","status",
            "observacao_atual","data_primeiro_contato","criado_em","origem",
            "retorno_apos_seq","data_agendamento","agendamento_pendente","proxima_acao"
        ])
    for col in [
        "documento","nome","telefone1","telefone2","telefone3","status",
        "observacao_atual","data_primeiro_contato","criado_em","origem",
        "retorno_apos_seq","data_agendamento","agendamento_pendente","proxima_acao"
    ]:
        if col not in df.columns:
            df[col] = None
    return df.sort_values("nome").reset_index(drop=True)

def carregar_contatos():
    dados = carregar_database()
    contatos_df = pd.DataFrame(dados["contatos"])
    empresas_df = carregar_empresas()

    if contatos_df.empty:
        return pd.DataFrame(columns=[
            "id","empresa_id","data_contato","tipo_contato","resultado","status_novo",
            "observacao","proxima_acao","data_proxima_acao","criado_em","seq_global",
            "nome","documento","telefone1","telefone2","telefone3"
        ])

    for col in [
        "id","empresa_id","data_contato","tipo_contato","resultado","status_novo",
        "observacao","proxima_acao","data_proxima_acao","criado_em","seq_global"
    ]:
        if col not in contatos_df.columns:
            contatos_df[col] = None

    if not empresas_df.empty:
        contatos_df = contatos_df.merge(
            empresas_df[["id","nome","documento","telefone1","telefone2","telefone3"]],
            left_on="empresa_id",
            right_on="id",
            how="left",
            suffixes=("", "_empresa")
        )
        if "id_empresa" in contatos_df.columns:
            contatos_df = contatos_df.drop(columns=["id_empresa"])

    contatos_df["_seq_sort"] = pd.to_numeric(contatos_df["seq_global"], errors="coerce").fillna(0)
    contatos_df = contatos_df.sort_values(
        ["_seq_sort","id"], ascending=[False,False]
    ).drop(columns=["_seq_sort"]).reset_index(drop=True)
    return contatos_df

def tentativas_empresa(empresa_id):
    dados = carregar_database()
    return sum(
        1 for c in dados["contatos"]
        if int(c.get("empresa_id", 0) or 0) == int(empresa_id)
        and c.get("tipo_contato") != "HISTÓRICO IMPORTADO"
    )

def salvar_empresa(documento, nome, telefones, status="SEM CONTATO", obs="", origem="APP"):
    dados = carregar_database(forcar_github=True)
    empresa_id = proximo_id(dados["empresas"])
    dados["empresas"].append({
        "id": empresa_id,
        "documento": formatar_documento(documento),
        "nome": nome.strip().upper(),
        "telefone1": formatar_telefone(telefones[0] if len(telefones)>0 else ""),
        "telefone2": formatar_telefone(telefones[1] if len(telefones)>1 else ""),
        "telefone3": formatar_telefone(telefones[2] if len(telefones)>2 else ""),
        "status": status,
        "observacao_atual": obs.strip(),
        "data_primeiro_contato": None,
        "criado_em": datetime.now().isoformat(timespec="seconds"),
        "origem": origem,
        "retorno_apos_seq": None,
        "data_agendamento": None,
        "agendamento_pendente": 0,
        "proxima_acao": ""
    })
    salvar_database(dados)

def registrar_contato(empresa_id, data_contato, tipo, resultado, obs,
                      proxima_acao="", data_agendamento=None):
    dados = carregar_database(forcar_github=True)
    agora = datetime.now().isoformat(timespec="seconds")

    tentativas_anteriores = sum(
        1 for c in dados["contatos"]
        if int(c.get("empresa_id", 0) or 0) == int(empresa_id)
        and c.get("tipo_contato") != "HISTÓRICO IMPORTADO"
        and c.get("resultado") == "NÃO CONSEGUI CONTATO"
    )
    tentativa_sem_retorno = tentativas_anteriores + 1 if resultado == "NÃO CONSEGUI CONTATO" else tentativas_anteriores

    seqs = [
        int(c.get("seq_global") or 0)
        for c in dados["contatos"]
        if c.get("seq_global") is not None
    ]
    seq = (max(seqs) if seqs else 0) + 1

    # Status automático conforme o resultado.
    if resultado == "NÃO CONSEGUI CONTATO":
        if tentativa_sem_retorno == 1:
            status_novo = "1ª TENTATIVA SEM RETORNO"
        elif tentativa_sem_retorno == 2:
            status_novo = "2ª TENTATIVA SEM RETORNO"
        else:
            status_novo = "SEM INTERESSE"
    else:
        status_novo = MAPA_STATUS.get(resultado, "EM ANDAMENTO")

    retorno_apos = None
    agendamento_pendente = 0
    data_agendamento_iso = None

    # Data específica sempre tem prioridade.
    if data_agendamento:
        data_agendamento_iso = data_agendamento.isoformat()
        agendamento_pendente = 1
        if status_novo not in STATUS_ENCERRADOS:
            status_novo = "RETORNO AGENDADO"

    # Se não houver data específica, todo status ativo volta após 200 contatos.
    elif status_novo not in STATUS_ENCERRADOS:
        retorno_apos = seq + 200

    dados["contatos"].append({
        "id": proximo_id(dados["contatos"]),
        "empresa_id": int(empresa_id),
        "data_contato": data_contato.isoformat(),
        "tipo_contato": tipo,
        "resultado": resultado,
        "status_novo": status_novo,
        "observacao": obs.strip(),
        "proxima_acao": proxima_acao.strip(),
        "data_proxima_acao": data_agendamento_iso,
        "criado_em": agora,
        "seq_global": seq,
        "usuario": st.session_state.get("usuario_logado", "")
    })

    for emp in dados["empresas"]:
        if int(emp.get("id", 0) or 0) == int(empresa_id):
            emp["status"] = status_novo
            emp["observacao_atual"] = obs.strip()
            if not emp.get("data_primeiro_contato"):
                emp["data_primeiro_contato"] = data_contato.isoformat()
            emp["retorno_apos_seq"] = retorno_apos
            emp["data_agendamento"] = data_agendamento_iso
            emp["agendamento_pendente"] = agendamento_pendente
            emp["proxima_acao"] = proxima_acao.strip()
            break

    salvar_database(dados)
    return status_novo, tentativa_sem_retorno, retorno_apos

def atualizar_empresa(empresa_id, nome, documento, telefone1, telefone2, telefone3,
                      status, observacao, proxima_acao, data_agendamento=None):
    """Salva qualquer edição do cadastro e sincroniza imediatamente no database.json."""
    dados = carregar_database(forcar_github=True)
    encontrado = False

    for emp in dados["empresas"]:
        if int(emp.get("id", 0) or 0) == int(empresa_id):
            emp["nome"] = str(nome or "").strip().upper()
            emp["documento"] = formatar_documento(documento)
            emp["telefone1"] = formatar_telefone(telefone1)
            emp["telefone2"] = formatar_telefone(telefone2)
            emp["telefone3"] = formatar_telefone(telefone3)
            emp["status"] = status
            emp["observacao_atual"] = str(observacao or "").strip()
            emp["proxima_acao"] = str(proxima_acao or "").strip()

            if data_agendamento:
                emp["data_agendamento"] = data_agendamento.isoformat()
                emp["agendamento_pendente"] = 1
                if status not in STATUS_ENCERRADOS:
                    emp["status"] = "RETORNO AGENDADO"
            elif status in STATUS_ENCERRADOS:
                emp["data_agendamento"] = None
                emp["agendamento_pendente"] = 0
                emp["retorno_apos_seq"] = None

            encontrado = True
            break

    if not encontrado:
        raise ValueError("Empresa não encontrada.")

    salvar_database(dados)


def atualizar_empresas_em_lote(df_editado):
    dados = carregar_database(forcar_github=True)
    mapa = {int(e.get("id", 0)): e for e in dados["empresas"]}

    for _, row in df_editado.iterrows():
        emp_id = int(row["ID"])
        emp = mapa.get(emp_id)
        if not emp:
            continue

        emp["documento"] = formatar_documento(row.get("CPF/CNPJ", ""))
        emp["nome"] = str(row.get("Empresa / Cliente", "") or "").strip().upper()
        emp["telefone1"] = formatar_telefone(row.get("Telefone 1", ""))
        emp["telefone2"] = formatar_telefone(row.get("Telefone 2", ""))
        emp["telefone3"] = formatar_telefone(row.get("Telefone 3", ""))
        emp["status"] = str(row.get("Status", "") or "").strip().upper()
        emp["observacao_atual"] = str(row.get("Última observação", "") or "").strip()
        emp["proxima_acao"] = str(row.get("Próxima ação", "") or "").strip()

        ag = row.get("Agendamento", "")
        if pd.notna(ag) and str(ag).strip():
            dt = pd.to_datetime(ag, dayfirst=True, errors="coerce")
            if pd.notna(dt):
                emp["data_agendamento"] = dt.date().isoformat()
                emp["agendamento_pendente"] = 1
        elif emp["status"] in STATUS_ENCERRADOS:
            emp["data_agendamento"] = None
            emp["agendamento_pendente"] = 0
            emp["retorno_apos_seq"] = None

    salvar_database(dados)

def atualizar_contatos_em_lote(df_editado):
    dados = carregar_database(forcar_github=True)
    mapa = {int(c.get("id", 0)): c for c in dados["contatos"]}

    for _, row in df_editado.iterrows():
        contato_id = int(row["ID contato"])
        c = mapa.get(contato_id)
        if not c:
            continue

        dt = pd.to_datetime(row.get("Data", ""), dayfirst=True, errors="coerce")
        if pd.notna(dt):
            c["data_contato"] = dt.date().isoformat()

        c["tipo_contato"] = str(row.get("Tipo", "") or "").strip().upper()
        c["resultado"] = str(row.get("Resultado", "") or "").strip().upper()
        c["status_novo"] = str(row.get("Status", "") or "").strip().upper()
        c["observacao"] = str(row.get("Observação", "") or "").strip()
        c["proxima_acao"] = str(row.get("Próxima ação", "") or "").strip()

        rt = pd.to_datetime(row.get("Retorno", ""), dayfirst=True, errors="coerce")
        c["data_proxima_acao"] = rt.date().isoformat() if pd.notna(rt) else None

    # Sincroniza a empresa com o contato operacional mais recente.
    ultimos = {}
    for c in dados["contatos"]:
        if c.get("tipo_contato") == "HISTÓRICO IMPORTADO":
            continue
        eid = int(c.get("empresa_id", 0) or 0)
        seq = int(c.get("seq_global") or 0)
        if eid not in ultimos or seq >= int(ultimos[eid].get("seq_global") or 0):
            ultimos[eid] = c

    for emp in dados["empresas"]:
        eid = int(emp.get("id", 0) or 0)
        if eid in ultimos:
            c = ultimos[eid]
            emp["status"] = c.get("status_novo") or emp.get("status")
            emp["observacao_atual"] = c.get("observacao") or ""
            emp["proxima_acao"] = c.get("proxima_acao") or ""
            emp["data_agendamento"] = c.get("data_proxima_acao")
            emp["agendamento_pendente"] = 1 if c.get("data_proxima_acao") else 0

    salvar_database(dados)

def editor_empresas(df_base, key_prefix):
    if df_base.empty:
        st.info("Nenhum registro encontrado.")
        return

    tabela = df_base[[
        "id","documento","nome","telefone1","telefone2","telefone3",
        "status","observacao_atual","data_primeiro_contato","proxima_acao","data_agendamento"
    ]].copy()

    tabela["data_primeiro_contato"] = tabela["data_primeiro_contato"].apply(data_br)
    tabela["data_agendamento"] = tabela["data_agendamento"].apply(data_br)
    tabela.columns = [
        "ID","CPF/CNPJ","Empresa / Cliente","Telefone 1","Telefone 2","Telefone 3",
        "Status","Última observação","1º contato","Próxima ação","Agendamento"
    ]

    status_opcoes = [
        "SEM CONTATO","1ª TENTATIVA SEM RETORNO","2ª TENTATIVA SEM RETORNO",
        "AGUARDANDO CLIENTE","RETORNO AGENDADO","EM ANDAMENTO",
        "REUNIÃO AGENDADA","PROPOSTA SOLICITADA","PROPOSTA ENVIADA",
        "EM NEGOCIAÇÃO","CONTATO INVÁLIDO","FECHADO / GANHO",
        "SEM INTERESSE","NÃO UTILIZA TRANSPORTE","JÁ UTILIZA AZUL"
    ]

    editado = st.data_editor(
        tabela,
        use_container_width=True,
        hide_index=True,
        num_rows="fixed",
        height=560,
        key=f"{key_prefix}_editor",
        column_config={
            "ID": st.column_config.NumberColumn("ID", disabled=True),
            "Status": st.column_config.SelectboxColumn(
                "Status", options=status_opcoes, required=True
            ),
        },
    )

    if st.button(
        "💾 Salvar alterações da tabela",
        type="primary",
        use_container_width=True,
        key=f"{key_prefix}_salvar"
    ):
        atualizar_empresas_em_lote(editado)
        st.success("Alterações salvas no database.json e atualizadas no app.")
        st.rerun()

def editor_contatos(df_base, key_prefix):
    if df_base.empty:
        st.info("Nenhum contato encontrado.")
        return

    tabela = df_base[[
        "id","data_contato","nome","documento","tipo_contato","resultado",
        "status_novo","observacao","proxima_acao","data_proxima_acao"
    ]].copy()

    tabela["data_contato"] = tabela["data_contato"].apply(data_br)
    tabela["data_proxima_acao"] = tabela["data_proxima_acao"].apply(data_br)
    tabela.columns = [
        "ID contato","Data","Empresa / Cliente","CPF/CNPJ","Tipo","Resultado",
        "Status","Observação","Próxima ação","Retorno"
    ]

    editado = st.data_editor(
        tabela,
        use_container_width=True,
        hide_index=True,
        num_rows="fixed",
        height=520,
        key=f"{key_prefix}_editor",
        column_config={
            "ID contato": st.column_config.NumberColumn("ID contato", disabled=True),
            "Empresa / Cliente": st.column_config.TextColumn("Empresa / Cliente", disabled=True),
            "CPF/CNPJ": st.column_config.TextColumn("CPF/CNPJ", disabled=True),
        },
    )

    if st.button(
        "💾 Salvar alterações da tabela",
        type="primary",
        use_container_width=True,
        key=f"{key_prefix}_salvar"
    ):
        atualizar_contatos_em_lote(editado)
        st.success("Alterações salvas no database.json.")
        st.rerun()

def obter_ultimo_contato_operacional():
    dados = carregar_database()
    contatos_ops = [
        c for c in dados["contatos"]
        if c.get("tipo_contato") != "HISTÓRICO IMPORTADO"
    ]
    if not contatos_ops:
        return None
    contatos_ops.sort(
        key=lambda c: (int(c.get("seq_global") or 0), int(c.get("id") or 0)),
        reverse=True
    )
    return contatos_ops[0]

def editar_ultimo_contato():
    ult = obter_ultimo_contato_operacional()
    if not ult:
        st.info("Ainda não há contato anterior para editar.")
        return

    dados = carregar_database()
    empresa = next(
        (e for e in dados["empresas"]
         if int(e.get("id", 0) or 0) == int(ult.get("empresa_id", 0) or 0)),
        None
    )
    if not empresa:
        st.warning("Cliente do último contato não encontrado.")
        return

    st.markdown(f"### ⬅️ Editar contato anterior — {empresa.get('nome','')}")
    st.caption("Altere o que faltou e salve. Isso atualiza o histórico e a situação atual do cliente.")

    tabela = pd.DataFrame([{
        "id": ult.get("id"),
        "data_contato": ult.get("data_contato"),
        "nome": empresa.get("nome"),
        "documento": empresa.get("documento"),
        "tipo_contato": ult.get("tipo_contato"),
        "resultado": ult.get("resultado"),
        "status_novo": ult.get("status_novo"),
        "observacao": ult.get("observacao"),
        "proxima_acao": ult.get("proxima_acao"),
        "data_proxima_acao": ult.get("data_proxima_acao"),
    }])

    editor_contatos(tabela, key_prefix=f"ultimo_contato_{ult.get('id')}")



def finalizar_sem_interesse(empresa_id):
    dados = carregar_database(forcar_github=True)
    agora = datetime.now().isoformat(timespec="seconds")

    seqs = [
        int(c.get("seq_global") or 0)
        for c in dados["contatos"]
        if c.get("seq_global") is not None
    ]
    seq = (max(seqs) if seqs else 0) + 1

    dados["contatos"].append({
        "id": proximo_id(dados["contatos"]),
        "empresa_id": int(empresa_id),
        "data_contato": date.today().isoformat(),
        "tipo_contato": "FINALIZAÇÃO RÁPIDA",
        "resultado": "SEM INTERESSE",
        "status_novo": "SEM INTERESSE",
        "observacao": "Finalizado manualmente na fila.",
        "proxima_acao": "",
        "data_proxima_acao": None,
        "criado_em": agora,
        "seq_global": seq,
        "usuario": st.session_state.get("usuario_logado", "")
    })

    for emp in dados["empresas"]:
        if int(emp.get("id", 0) or 0) == int(empresa_id):
            emp["status"] = "SEM INTERESSE"
            emp["observacao_atual"] = "Finalizado manualmente na fila."
            emp["proxima_acao"] = ""
            emp["retorno_apos_seq"] = None
            emp["data_agendamento"] = None
            emp["agendamento_pendente"] = 0
            break

    salvar_database(dados)


# -----------------------------
# IMPORTAÇÃO LIVRE / EM LOTE
# -----------------------------
DOC_REGEX = re.compile(r'(?<!\d)(\d{11}|\d{14})(?!\d)')
PHONE_REGEX = re.compile(
    r'(?:\(?\d{2}\)?[\s\-]*)?(?:9?\d{4})[\s\-]?\d{4}'
)

def extrair_telefones(texto):
    encontrados = []
    for m in PHONE_REGEX.findall(texto):
        d = somente_digitos(m)
        # Evita confundir CPF/CNPJ como telefone.
        if len(d) in (10, 11):
            fmt = formatar_telefone(d)
            if fmt not in encontrados:
                encontrados.append(fmt)
    return encontrados[:3]

def limpar_nome_linha(linha):
    s = linha
    # Remove docs
    for d in DOC_REGEX.findall(s):
        s = s.replace(d, " ")
    # Remove telefones detectados
    for p in PHONE_REGEX.findall(s):
        s = s.replace(p, " ")
    s = re.sub(r'\s+', ' ', s).strip(" -;|,\t")
    return s.strip()

def parsear_texto_livre(texto):
    linhas = [re.sub(r'\s+', ' ', l).strip() for l in texto.splitlines()]
    linhas = [l for l in linhas if l]

    registros = []
    atual = None

    for linha in linhas:
        docs = DOC_REGEX.findall(linha)
        telefones = extrair_telefones(linha)
        nome = limpar_nome_linha(linha)

        # Linha que começa claramente um novo registro.
        inicia_novo = bool(docs) or (nome and telefones)

        if inicia_novo:
            if atual:
                registros.append(atual)
            atual = {
                "documento": docs[0] if docs else "",
                "nome": nome,
                "telefones": telefones[:],
            }
        else:
            # Linha de continuação, normalmente telefone quebrado.
            if atual and telefones:
                for tel in telefones:
                    if tel not in atual["telefones"] and len(atual["telefones"]) < 3:
                        atual["telefones"].append(tel)
            elif atual and nome and not atual["nome"]:
                atual["nome"] = nome
            elif nome:
                if atual:
                    registros.append(atual)
                atual = {"documento": "", "nome": nome, "telefones": []}

    if atual:
        registros.append(atual)

    saida = []
    for r in registros:
        # Se não há nome, usa uma identificação neutra para permitir revisão antes de incluir.
        saida.append({
            "CPF/CNPJ": formatar_documento(r["documento"]) if r["documento"] else "",
            "Nome": r["nome"].upper().strip(),
            "Telefone 1": r["telefones"][0] if len(r["telefones"]) > 0 else "",
            "Telefone 2": r["telefones"][1] if len(r["telefones"]) > 1 else "",
            "Telefone 3": r["telefones"][2] if len(r["telefones"]) > 2 else "",
        })

    return pd.DataFrame(saida)

def eh_duplicado(documento, telefones, empresas):
    doc = somente_digitos(documento)
    if doc:
        docs = empresas["documento"].fillna("").map(somente_digitos)
        if docs.eq(doc).any():
            return True

    tels_novos = {somente_digitos(t) for t in telefones if somente_digitos(t)}
    if tels_novos:
        for col in ["telefone1","telefone2","telefone3"]:
            existentes = set(empresas[col].fillna("").map(somente_digitos))
            if tels_novos & existentes:
                return True
    return False


def painel_edicao_empresa(empresa, prefixo="editar"):
    """Painel reutilizável para editar cadastro/status em qualquer tela."""
    empresa_id = int(empresa["id"])
    with st.expander("✏️ Editar dados deste cliente", expanded=False):
        c1, c2 = st.columns(2)
        nome = c1.text_input(
            "Empresa / Cliente",
            value=str(empresa.get("nome") or ""),
            key=f"{prefixo}_{empresa_id}_nome"
        )
        documento = c2.text_input(
            "CPF/CNPJ",
            value=str(empresa.get("documento") or ""),
            key=f"{prefixo}_{empresa_id}_documento"
        )

        c1, c2, c3 = st.columns(3)
        tel1 = c1.text_input(
            "Telefone 1",
            value=str(empresa.get("telefone1") or ""),
            key=f"{prefixo}_{empresa_id}_tel1"
        )
        tel2 = c2.text_input(
            "Telefone 2",
            value=str(empresa.get("telefone2") or ""),
            key=f"{prefixo}_{empresa_id}_tel2"
        )
        tel3 = c3.text_input(
            "Telefone 3",
            value=str(empresa.get("telefone3") or ""),
            key=f"{prefixo}_{empresa_id}_tel3"
        )

        status_opcoes = [
            "SEM CONTATO",
            "1ª TENTATIVA SEM RETORNO",
            "2ª TENTATIVA SEM RETORNO",
            "AGUARDANDO CLIENTE",
            "RETORNO AGENDADO",
            "EM ANDAMENTO",
            "REUNIÃO AGENDADA",
            "PROPOSTA SOLICITADA",
            "PROPOSTA ENVIADA",
            "EM NEGOCIAÇÃO",
            "CONTATO INVÁLIDO",
            "FECHADO / GANHO",
            "SEM INTERESSE",
            "NÃO UTILIZA TRANSPORTE",
            "JÁ UTILIZA AZUL",
        ]
        atual_status = str(empresa.get("status") or "SEM CONTATO")
        if atual_status not in status_opcoes:
            status_opcoes.insert(1, atual_status)

        status = st.selectbox(
            "Status",
            status_opcoes,
            index=status_opcoes.index(atual_status),
            key=f"{prefixo}_{empresa_id}_status"
        )

        observacao = st.text_area(
            "Última observação",
            value=str(empresa.get("observacao_atual") or ""),
            key=f"{prefixo}_{empresa_id}_obs"
        )
        proxima_acao = st.text_input(
            "Próxima ação",
            value=str(empresa.get("proxima_acao") or ""),
            key=f"{prefixo}_{empresa_id}_acao"
        )

        tem_agendamento_atual = bool(empresa.get("data_agendamento"))
        usar_data = st.checkbox(
            "Manter/definir data específica de retorno",
            value=tem_agendamento_atual,
            key=f"{prefixo}_{empresa_id}_usar_data"
        )
        data_ag = None
        if usar_data:
            valor_data = date.today()
            if empresa.get("data_agendamento"):
                try:
                    valor_data = pd.to_datetime(empresa.get("data_agendamento")).date()
                except Exception:
                    pass
            data_ag = st.date_input(
                "Data de retorno",
                value=valor_data,
                format="DD/MM/YYYY",
                key=f"{prefixo}_{empresa_id}_data"
            )

        if st.button(
            "💾 Salvar edição",
            type="primary",
            use_container_width=True,
            key=f"{prefixo}_{empresa_id}_salvar"
        ):
            atualizar_empresa(
                empresa_id, nome, documento, tel1, tel2, tel3,
                status, observacao, proxima_acao, data_ag
            )
            st.success("Dados atualizados no database.json.")
            st.rerun()


# -----------------------------
# VISUAL
# -----------------------------
def card(titulo, valor, subtitulo=""):
    st.markdown(
        f"""
        <div style="
            border:1px solid #e5e7eb;
            border-radius:16px;
            padding:18px 16px;
            min-height:118px;
            background:white;
            box-shadow:0 2px 8px rgba(0,0,0,.05);
        ">
            <div style="font-size:14px;color:#666;margin-bottom:8px;">{titulo}</div>
            <div style="font-size:30px;font-weight:700;line-height:1.1;">{valor}</div>
            <div style="font-size:12px;color:#888;margin-top:8px;">{subtitulo}</div>
        </div>
        """,
        unsafe_allow_html=True
    )

def grafico_contatos_dia(df):
    if df.empty:
        st.info("Ainda não há contatos registrados no período.")
        return

    diario = df.groupby("data_dt").size().reset_index(name="Quantidade")
    diario["Data"] = diario["data_dt"].apply(lambda d: d.strftime("%d/%m"))

    base = alt.Chart(diario).encode(
        x=alt.X("Data:N", sort=None, title="Data"),
        y=alt.Y("Quantidade:Q", title="Quantidade de contatos"),
        tooltip=["Data:N", "Quantidade:Q"]
    )

    barras = base.mark_bar()
    textos = base.mark_text(dy=-10).encode(text="Quantidade:Q")
    st.altair_chart(barras + textos, use_container_width=True)

def historico_cliente(contatos, empresa_id):
    hist = contatos[contatos["empresa_id"] == empresa_id].copy()
    if hist.empty:
        st.info("Nenhum histórico registrado.")
        return
    editor_contatos(hist, key_prefix=f"historico_{empresa_id}")

# -----------------------------
# EXPORTAÇÃO COMPLETA
# -----------------------------
def gerar_excel_completo(empresas, contatos):
    buffer = io.BytesIO()

    carteira = empresas.copy()
    carteira["Data 1º contato"] = carteira["data_primeiro_contato"].apply(data_br)
    carteira["Data agendamento"] = carteira["data_agendamento"].apply(data_br)
    carteira_export = carteira[[
        "id","documento","nome","telefone1","telefone2","telefone3","status",
        "observacao_atual","Data 1º contato","proxima_acao","Data agendamento",
        "agendamento_pendente","retorno_apos_seq","origem","criado_em"
    ]].copy()
    carteira_export.columns = [
        "ID","CPF/CNPJ","Empresa / Cliente","Telefone 1","Telefone 2","Telefone 3",
        "Status atual","Última observação","Data 1º contato","Próxima ação",
        "Data agendada","Agendamento pendente","Retorno após sequência",
        "Origem","Criado em"
    ]

    hist = contatos.copy()
    if not hist.empty:
        hist["Data contato"] = hist["data_contato"].apply(data_br)
        hist["Data retorno"] = hist["data_proxima_acao"].apply(data_br)
        hist_export = hist[[
            "id","empresa_id","nome","documento","telefone1","telefone2","telefone3",
            "Data contato","tipo_contato","resultado","status_novo","observacao",
            "proxima_acao","Data retorno","seq_global","criado_em"
        ]].copy()
        hist_export.columns = [
            "ID contato","ID empresa","Empresa / Cliente","CPF/CNPJ","Telefone 1",
            "Telefone 2","Telefone 3","Data contato","Tipo contato","Resultado",
            "Status após contato","Observação","Próxima ação","Data retorno",
            "Sequência global","Registrado em"
        ]
    else:
        hist_export = pd.DataFrame()

    hoje = date.today()
    pend = carteira.copy()
    pend["ag_dt"] = pd.to_datetime(pend["data_agendamento"], errors="coerce").dt.date
    seq_atual = seq_global_atual()

    pend["Tipo pendência"] = ""
    pend.loc[
        (pend["agendamento_pendente"] == 1) & pend["ag_dt"].notna(),
        "Tipo pendência"
    ] = "AGENDAMENTO"
    pend.loc[
        pend["retorno_apos_seq"].notna() & (pend["retorno_apos_seq"] <= seq_atual),
        "Tipo pendência"
    ] = "RETORNO AUTOMÁTICO"

    pend = pend[pend["Tipo pendência"] != ""].copy()
    pend["Data"] = pend["data_agendamento"].apply(data_br)
    pend_export = pend[[
        "documento","nome","telefone1","status","Tipo pendência",
        "Data","proxima_acao","observacao_atual"
    ]].copy()
    pend_export.columns = [
        "CPF/CNPJ","Empresa / Cliente","Telefone","Status","Tipo pendência",
        "Data","Próxima ação","Observação"
    ]

    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        carteira_export.to_excel(writer, index=False, sheet_name="Carteira atual")
        hist_export.to_excel(writer, index=False, sheet_name="Histórico de contatos")
        pend_export.to_excel(writer, index=False, sheet_name="Pendências e retornos")

        # Resumo
        reais = contatos.copy() if not contatos.empty else pd.DataFrame()
        resumo = pd.DataFrame({
            "Indicador": [
                "Empresas na carteira",
                "Contatos registrados",
                "Empresas sem contato",
                "Aguardando cliente",
                "Em andamento / negociação",
                "Fechados",
                "Sem interesse"
            ],
            "Quantidade": [
                len(empresas),
                len(reais),
                int((empresas["status"]=="SEM CONTATO").sum()),
                int((empresas["status"]=="AGUARDANDO CLIENTE").sum()),
                int(empresas["status"].isin(["EM ANDAMENTO","NEGOCIAÇÃO","PROPOSTA ENVIADA","REUNIÃO AGENDADA"]).sum()),
                int((empresas["status"]=="FECHADO").sum()),
                int((empresas["status"]=="SEM INTERESSE").sum()),
            ]
        })
        resumo.to_excel(writer, index=False, sheet_name="Resumo")

    buffer.seek(0)
    return buffer.getvalue()

# -----------------------------
# APP
# -----------------------------
# Sempre sincroniza a base oficial antes de abrir o sistema.
if github_ativo():
    carregar_database(forcar_github=True)

criar_banco()
importar_planilha_inicial()

empresas = carregar_empresas()
contatos = carregar_contatos()



# -----------------------------
# BASE PERSISTENTE OBRIGATÓRIA
# -----------------------------
if not github_ativo():
    st.error(
        "⚠️ A base persistente do GitHub não está conectada. "
        "Para proteger os dados, o sistema está bloqueado para gravações. "
        "Configure GITHUB_TOKEN, GITHUB_REPO, GITHUB_DATA_BRANCH e GITHUB_DB_PATH nos Secrets."
    )

# -----------------------------
# LOGIN
# -----------------------------
def credenciais():
    """
    Usuários ficam nos Secrets do Streamlit.
    Exemplo:
    [usuarios]
    celso = "..."
    jessica = "..."
    vanessa = "..."
    """
    try:
        usuarios = dict(st.secrets["usuarios"])
        return {str(k).lower(): str(v) for k, v in usuarios.items()}
    except Exception:
        return {}

if "autenticado" not in st.session_state:
    st.session_state.autenticado = False

if not st.session_state.autenticado:
    st.title("🔐 Gestão Comercial")
    st.caption("Acesso ao sistema")

    with st.form("login"):
        usuario = st.text_input("Usuário").strip().lower()
        senha = st.text_input("Senha", type="password")
        entrar = st.form_submit_button("Entrar", type="primary", use_container_width=True)

    if entrar:
        usuarios = credenciais()
        if usuario in usuarios and senha == usuarios[usuario]:
            st.session_state.autenticado = True
            st.session_state.usuario_logado = usuario
            st.rerun()
        else:
            st.error("Usuário ou senha inválidos.")
    st.stop()

st.sidebar.write(f"👤 **{st.session_state.get('usuario_logado','').title()}**")
if st.sidebar.button("Sair", use_container_width=True):
    st.session_state.clear()
    st.rerun()


st.title("📈 Gestão Comercial")
st.caption("Prospecção, retornos, agendamentos e acompanhamento da carteira comercial.")

menu = st.sidebar.radio(
    "Menu",
    [
        "📊 Dashboard",
        "📞 Fila de contatos",
        "➕ Adicionar contatos em lote",
        "🏢 Consulta / Editar Clientes",
        "➕ Nova Empresa",
        "📈 Relatórios",
    ]
)

# ---------------- DASHBOARD ----------------
if menu == "📊 Dashboard":
    hoje = date.today()

    # Para análise gerencial, considera também o histórico válido importado da planilha.
    analitico = contatos.copy()
    if not analitico.empty:
        analitico["data_dt"] = pd.to_datetime(
            analitico["data_contato"], errors="coerce"
        ).dt.date
        analitico = analitico[analitico["data_dt"].notna()].copy()

    st.subheader("Visão de desempenho")

    cperiodo, cdata = st.columns([1, 2])
    periodo = cperiodo.selectbox(
        "Período",
        ["Dia", "Últimos 7 dias", "Últimos 15 dias", "Mês", "Período personalizado"]
    )

    data_ref = hoje
    inicio = hoje
    fim = hoje

    if periodo == "Dia":
        data_ref = cdata.date_input(
            "Data da análise",
            value=hoje,
            max_value=hoje,
            format="DD/MM/YYYY"
        )
        inicio = fim = data_ref

    elif periodo == "Últimos 7 dias":
        data_ref = cdata.date_input(
            "Data final",
            value=hoje,
            max_value=hoje,
            format="DD/MM/YYYY"
        )
        fim = data_ref
        inicio = fim - timedelta(days=6)

    elif periodo == "Últimos 15 dias":
        data_ref = cdata.date_input(
            "Data final",
            value=hoje,
            max_value=hoje,
            format="DD/MM/YYYY"
        )
        fim = data_ref
        inicio = fim - timedelta(days=14)

    elif periodo == "Mês":
        data_ref = cdata.date_input(
            "Escolha uma data do mês",
            value=hoje,
            max_value=hoje,
            format="DD/MM/YYYY"
        )
        inicio = data_ref.replace(day=1)
        if data_ref.month == 12:
            prox = date(data_ref.year + 1, 1, 1)
        else:
            prox = date(data_ref.year, data_ref.month + 1, 1)
        fim = min(prox - timedelta(days=1), hoje)

    else:
        cini, cfim = st.columns(2)
        inicio = cini.date_input(
            "De",
            value=hoje - timedelta(days=30),
            max_value=hoje,
            format="DD/MM/YYYY",
            key="dash_inicio"
        )
        fim = cfim.date_input(
            "Até",
            value=hoje,
            max_value=hoje,
            format="DD/MM/YYYY",
            key="dash_fim"
        )
        if inicio > fim:
            st.error("A data inicial não pode ser maior que a data final.")
            st.stop()

    if not analitico.empty:
        selecionado = analitico[
            (analitico["data_dt"] >= inicio) &
            (analitico["data_dt"] <= fim)
        ].copy()
    else:
        selecionado = pd.DataFrame()

    total_contatos = len(selecionado)
    empresas_periodo = selecionado["empresa_id"].nunique() if not selecionado.empty else 0
    dias_periodo = max(1, (fim - inicio).days + 1)
    media_dia = round(total_contatos / dias_periodo, 1)

    reunioes = int((selecionado["resultado"] == "REUNIÃO AGENDADA").sum()) if not selecionado.empty else 0
    propostas = int((selecionado["resultado"].isin(["PROPOSTA SOLICITADA","PROPOSTA ENVIADA"])).sum()) if not selecionado.empty else 0
    fechados_periodo = int((selecionado["resultado"] == "FECHADO").sum()) if not selecionado.empty else 0
    sem_retorno_periodo = int((selecionado["resultado"].isin(["SEM RETORNO","SEM SUCESSO NO CONTATO"])).sum()) if not selecionado.empty else 0

    c1,c2,c3,c4 = st.columns(4)
    with c1:
        card("Contatos no período", total_contatos, f"{inicio.strftime('%d/%m/%Y')} a {fim.strftime('%d/%m/%Y')}")
    with c2:
        card("Empresas trabalhadas", empresas_periodo, "clientes diferentes")
    with c3:
        card("Média por dia", media_dia, "contatos/dia")
    with c4:
        card("Sem retorno", sem_retorno_periodo, "no período selecionado")

    st.write("")
    c1,c2,c3,c4 = st.columns(4)
    with c1:
        card("Reuniões", reunioes, "agendadas no período")
    with c2:
        card("Propostas", propostas, "solicitadas/enviadas")
    with c3:
        card("Fechamentos", fechados_periodo, "no período")
    with c4:
        card("Carteira total", len(empresas), "empresas/clientes")

    st.divider()
    st.subheader("Contatos realizados por dia")
    grafico_contatos_dia(selecionado)

    if not selecionado.empty:
        resumo_diario = selecionado.groupby("data_dt").agg(
            Contatos=("id", "count"),
            Empresas=("empresa_id", "nunique")
        ).reset_index()
        resumo_diario["Data"] = resumo_diario["data_dt"].apply(
            lambda d: d.strftime("%d/%m/%Y")
        )
        resumo_diario = resumo_diario[["Data","Contatos","Empresas"]].iloc[::-1]
        st.dataframe(resumo_diario, use_container_width=True, hide_index=True)
    else:
        st.info("Nenhum contato encontrado nesse período.")

    st.divider()
    st.subheader("Visão geral do histórico")
    if analitico.empty:
        st.info("Ainda não há histórico de contatos.")
    else:
        hist = analitico.copy()
        hist["Ano"] = hist["data_dt"].apply(lambda d: d.year)
        hist["Mes"] = hist["data_dt"].apply(lambda d: d.month)
        nomes_meses = {
            1:"Janeiro",2:"Fevereiro",3:"Março",4:"Abril",5:"Maio",6:"Junho",
            7:"Julho",8:"Agosto",9:"Setembro",10:"Outubro",11:"Novembro",12:"Dezembro"
        }

        total_geral = len(hist)
        st.metric("Total geral de contatos", total_geral)

        meses = hist.groupby(["Ano","Mes"]).size().reset_index(name="Contatos")
        meses = meses.sort_values(["Ano","Mes"], ascending=[False,False])

        for _, m in meses.iterrows():
            ano = int(m["Ano"])
            mes = int(m["Mes"])
            qtd = int(m["Contatos"])
            with st.expander(f"{nomes_meses[mes]}/{ano} — {qtd} contato(s)"):
                mensal = hist[(hist["Ano"]==ano) & (hist["Mes"]==mes)].copy()
                diario = mensal.groupby("data_dt").agg(
                    Contatos=("id","count"),
                    Empresas=("empresa_id","nunique")
                ).reset_index()
                diario["Data"] = diario["data_dt"].apply(lambda d: d.strftime("%d/%m/%Y"))
                diario = diario[["Data","Contatos","Empresas"]]
                st.dataframe(diario, use_container_width=True, hide_index=True)

    st.subheader("Situação atual da carteira")
    if not empresas.empty:
        status_tbl = empresas["status"].value_counts().rename_axis("Status").reset_index(name="Quantidade")
        st.dataframe(status_tbl, use_container_width=True, hide_index=True)

        with st.expander("✏️ Edição rápida de cliente"):
            busca_dash = st.text_input(
                "Buscar cliente para editar",
                key="dash_busca_edicao"
            )
            if busca_dash.strip():
                termo = busca_dash.strip().lower()
                candidatos = empresas[
                    empresas["nome"].fillna("").str.lower().str.contains(termo, regex=False)
                    | empresas["documento"].fillna("").str.lower().str.contains(termo, regex=False)
                    | empresas["telefone1"].fillna("").str.lower().str.contains(termo, regex=False)
                ].head(20)
                if candidatos.empty:
                    st.info("Nenhum cliente encontrado.")
                else:
                    mapa = {
                        f"{r['nome']} — {r['documento'] or r['telefone1'] or ''}": int(r["id"])
                        for _, r in candidatos.iterrows()
                    }
                    esc = st.selectbox(
                        "Cliente",
                        list(mapa.keys()),
                        key="dash_cliente_edicao"
                    )
                    emp_row = empresas[empresas["id"] == mapa[esc]].iloc[0]
                    painel_edicao_empresa(emp_row, prefixo="dash_editar")

# ---------------- FILA ÚNICA DE CONTATOS ----------------
elif menu == "📞 Fila de contatos":
    st.subheader("📞 Fila de contatos")
    st.caption("Aqui estão reunidos novos contatos, retornos antigos e pendências. O sistema organiza automaticamente quem deve ser atendido primeiro.")

    hoje = date.today()
    hoje_ts = pd.Timestamp(hoje)
    seq_atual = seq_global_atual()

    base = empresas.copy()
    base["ag_dt"] = pd.to_datetime(
        base["data_agendamento"], errors="coerce"
    ).dt.normalize()

    # Identifica empresas que já tiveram alguma tentativa REAL dentro do novo app.
    if not contatos.empty:
        operacionais = contatos[
            contatos["tipo_contato"] != "HISTÓRICO IMPORTADO"
        ].copy()
        ids_trabalhados_app = set(
            pd.to_numeric(operacionais["empresa_id"], errors="coerce")
            .dropna().astype(int).tolist()
        )
    else:
        ids_trabalhados_app = set()

    # 1) Agendamentos atrasados.
    atrasados = base[
        (base["agendamento_pendente"] == 1) &
        base["ag_dt"].notna() &
        (base["ag_dt"] < hoje_ts) &
        (~base["status"].isin(STATUS_ENCERRADOS))
    ].copy()

    # 2) Agendamentos de hoje.
    hoje_ag = base[
        (base["agendamento_pendente"] == 1) &
        base["ag_dt"].notna() &
        (base["ag_dt"] == hoje_ts) &
        (~base["status"].isin(STATUS_ENCERRADOS))
    ].copy()

    # 3) Retornos automáticos liberados após 200 contatos.
    automaticos = base[
        base["retorno_apos_seq"].notna() &
        (pd.to_numeric(base["retorno_apos_seq"], errors="coerce") <= seq_atual) &
        (~base["status"].isin(STATUS_ENCERRADOS))
    ].copy()

    # 4) Carteira antiga importada que ainda precisa de continuidade.
    # Só entram aqui os registros que ainda NÃO foram trabalhados dentro do novo app.
    retornos_importados = base[
        base["status"].isin(STATUS_RETORNO_IMPORTADO) &
        (~base["id"].astype(int).isin(ids_trabalhados_app)) &
        (base["agendamento_pendente"].fillna(0) != 1) &
        (base["retorno_apos_seq"].isna())
    ].copy()

    # 5) Novos sem qualquer contato.
    novos = base[
        (base["status"] == "SEM CONTATO") &
        (base["agendamento_pendente"].fillna(0) != 1)
    ].copy()

    # Cards sempre visíveis.
    c1,c2,c3,c4,c5 = st.columns(5)
    with c1:
        card("🔴 Atrasados", len(atrasados), "permanecem até serem tratados")
    with c2:
        card("🟠 Para hoje", len(hoje_ag), "agendamentos do dia")
    with c3:
        card("🔄 Retornos", len(automaticos), "liberados após 200 contatos")
    with c4:
        card("📂 Retornos antigos", len(retornos_importados), "carteira importada pendente")
    with c5:
        card("🆕 Novos", len(novos), "ainda sem contato")

    # Ordem:
    # atrasados > hoje > novos > retorno automático > retorno antigo
    atrasados["_prioridade"] = 1
    hoje_ag["_prioridade"] = 2
    novos["_prioridade"] = 3
    automaticos["_prioridade"] = 4
    retornos_importados["_prioridade"] = 5

    fila_df = pd.concat(
        [atrasados, hoje_ag, novos, automaticos, retornos_importados],
        ignore_index=True
    ).drop_duplicates(subset=["id"], keep="first")

    if fila_df.empty:
        st.success("Não há contatos pendentes na fila.")
    else:
        # Nos retornos antigos, prioriza quem teve contato mais antigo primeiro.
        fila_df["_data_primeiro"] = pd.to_datetime(
            fila_df["data_primeiro_contato"], errors="coerce"
        )

        fila_df = fila_df.sort_values(
            ["_prioridade", "ag_dt", "_data_primeiro", "nome"],
            na_position="last"
        ).reset_index(drop=True)

        atual = fila_df.iloc[0]
        empresa_id = int(atual["id"])
        tent = tentativas_empresa(empresa_id) + 1

        msg = st.session_state.pop("flash_contato", None)
        if msg:
            st.success(msg)

        if st.session_state.get("mostrar_ultimo_contato"):
            with st.container(border=True):
                editar_ultimo_contato()
                if st.button(
                    "Fechar edição do contato anterior",
                    use_container_width=True,
                    key="fechar_edicao_anterior"
                ):
                    st.session_state["mostrar_ultimo_contato"] = False
                    st.rerun()
            st.divider()

        # Identificação clara do tipo de item da fila.
        if atual["_prioridade"] == 1:
            st.error(
                f"🔴 PRIORIDADE — retorno atrasado desde "
                f"{atual['ag_dt'].strftime('%d/%m/%Y')}"
            )
        elif atual["_prioridade"] == 2:
            st.warning("🟠 PRIORIDADE — retorno agendado para hoje")
        elif atual["_prioridade"] == 3:
            st.info("🆕 NOVO CONTATO — ainda sem contato anterior")
        elif atual["_prioridade"] == 4:
            st.info("🔄 RETORNO AUTOMÁTICO — cliente liberado após 200 novos contatos")
        else:
            st.info(
                f"📂 RETORNO DA CARTEIRA ANTIGA — status atual: {atual['status']}"
            )

        c1,c2 = st.columns([4,1])
        with c1:
            st.markdown(f"### {atual['nome']}")
            st.write(f"**CPF/CNPJ:** {atual['documento'] or '-'}")

            telefones = [
                t for t in [
                    atual["telefone1"], atual["telefone2"], atual["telefone3"]
                ]
                if t and str(t).lower() != "nan"
            ]
            st.write(
                f"**Telefone(s):** {' | '.join(telefones) if telefones else '-'}"
            )
            st.write(f"**Status atual:** {atual['status']}")

            if atual["agendamento_pendente"] == 1 and pd.notna(atual["ag_dt"]):
                st.write(
                    f"**Retorno agendado:** "
                    f"{atual['ag_dt'].strftime('%d/%m/%Y')}"
                )
                if atual["proxima_acao"]:
                    st.write(f"**Ação prevista:** {atual['proxima_acao']}")

        with c2:
            card(
                "Na fila",
                f"1/{len(fila_df)}",
                "próximo da prioridade"
            )

        if atual["observacao_atual"]:
            st.info(
                f"**Última observação:** {atual['observacao_atual']}"
            )

        painel_edicao_empresa(atual, prefixo="fila_editar")

        hist_cliente = contatos[
            contatos["empresa_id"] == empresa_id
        ] if not contatos.empty else pd.DataFrame()

        if not hist_cliente.empty:
            with st.expander("Ver histórico deste cliente"):
                historico_cliente(contatos, empresa_id)

        # Cada cliente usa chaves próprias; ao avançar, o próximo vem limpo.
        prefixo = f"fila_{empresa_id}"

        st.markdown(f"**Tentativa {min(tent, 3)} de 3**")

        c1,c2 = st.columns(2)
        data_contato = c1.date_input(
            "Data do contato *",
            value=hoje,
            max_value=hoje,
            format="DD/MM/YYYY",
            key=f"{prefixo}_data"
        )
        tipo = c2.selectbox(
            "Tipo de contato *",
            TIPOS_CONTATO,
            key=f"{prefixo}_tipo"
        )

        resultado = st.selectbox(
            "Resultado do contato *",
            RESULTADOS,
            key=f"{prefixo}_resultado"
        )

        obs = st.text_area(
            "Observação do contato",
            value="",
            key=f"{prefixo}_obs"
        )

        acao_escolhida = st.selectbox(
            "Próxima ação",
            ACOES_SUGERIDAS,
            key=f"{prefixo}_acao"
        )

        if acao_escolhida == "OUTRO":
            acao = st.text_input(
                "Descreva a próxima ação",
                value="",
                key=f"{prefixo}_outra_acao"
            ).strip()
        else:
            acao = acao_escolhida

        st.caption(
            "Resultados de espera retornam automaticamente à fila após 200 novos contatos. "
            "Na 3ª tentativa sem contato, o cliente é encerrado automaticamente como SEM INTERESSE."
        )

        agendar = st.checkbox(
            "Definir uma data específica para retorno",
            value=False,
            key=f"{prefixo}_agendar"
        )

        data_ag = None
        if agendar:
            data_ag = st.date_input(
                "Data específica do retorno",
                value=hoje + timedelta(days=1),
                min_value=hoje,
                format="DD/MM/YYYY",
                key=f"{prefixo}_data_agendamento"
            )
            st.caption(
                "A partir dessa data, o cliente ficará no topo da fila até ser atualizado."
            )

        bvoltar, bfinalizar, bsalvar = st.columns([1, 1, 2])

        with bvoltar:
            voltar_anterior = st.button(
                "⬅️ Voltar ao anterior",
                use_container_width=True,
                key=f"{prefixo}_voltar_anterior"
            )

        with bfinalizar:
            finalizar = st.button(
                "🚫 Finalizar / Sem interesse",
                use_container_width=True,
                key=f"{prefixo}_finalizar"
            )

        with bsalvar:
            salvar = st.button(
                "Salvar e ir para o próximo",
                type="primary",
                use_container_width=True,
                key=f"{prefixo}_salvar"
            )

        if voltar_anterior:
            st.session_state["mostrar_ultimo_contato"] = True
            st.rerun()

        if finalizar:
            finalizar_sem_interesse(empresa_id)
            st.session_state["flash_contato"] = (
                f"{atual['nome']}: finalizado como SEM INTERESSE e removido da fila."
            )
            for chave in list(st.session_state.keys()):
                if chave.startswith(prefixo):
                    del st.session_state[chave]
            st.rerun()

        if salvar:
            status_novo, tentativa_atual, retorno_apos = registrar_contato(
                empresa_id, data_contato, tipo, resultado, obs, acao, data_ag
            )

            if status_novo == "SEM INTERESSE" and resultado == "NÃO CONSEGUI CONTATO":
                mensagem = (
                    f"{atual['nome']}: 3ª tentativa sem retorno. "
                    "Cliente encerrado automaticamente como SEM INTERESSE."
                )
            elif data_ag:
                mensagem = (
                    f"{atual['nome']}: contato salvo. "
                    f"Retorno agendado para {data_ag.strftime('%d/%m/%Y')}."
                )
            elif retorno_apos:
                mensagem = (
                    f"{atual['nome']}: contato salvo. "
                    "Voltará automaticamente após 200 novos contatos."
                )
            else:
                mensagem = f"{atual['nome']}: contato salvo com sucesso."

            st.session_state["flash_contato"] = mensagem

            for chave in list(st.session_state.keys()):
                if chave.startswith(prefixo):
                    del st.session_state[chave]

            st.rerun()

# ---------------- IMPORTAÇÃO EM LOTE ----------------
elif menu == "➕ Adicionar contatos em lote":
    st.subheader("➕ Adicionar contatos em lote")
    st.caption(
        "Cole os contatos do jeito que você recebeu. O sistema tentará identificar "
        "nome, CPF/CNPJ e telefones e mostrará uma prévia antes de incluir."
    )

    bruto = st.text_area(
        "Cole ou digite os contatos aqui",
        height=300,
        placeholder=(
            "23239909855    ID HERISOA RALITERA    (11) 99400-2761    (11) 94109-3300\n"
            "                                      (11) 3023-3412\n\n"
            "OUTRA EMPRESA  12.345.678/0001-90  (19) 99999-9999"
        )
    )

    if bruto.strip():
        previa = parsear_texto_livre(bruto)
        if previa.empty:
            st.warning("Não consegui identificar registros nesse texto.")
        else:
            previa["Duplicado"] = previa.apply(
                lambda r: "SIM" if eh_duplicado(
                    r["CPF/CNPJ"],
                    [r["Telefone 1"],r["Telefone 2"],r["Telefone 3"]],
                    empresas
                ) else "NÃO",
                axis=1
            )

            st.markdown(f"### Prévia — {len(previa)} registro(s) identificado(s)")
            st.dataframe(previa, use_container_width=True, hide_index=True)

            incluir = st.button("Adicionar novos contatos à carteira", type="primary")
            if incluir:
                incluidos = 0
                ignorados = 0
                invalidos = 0

                for _, r in previa.iterrows():
                    nome = str(r["Nome"] or "").strip()
                    doc = str(r["CPF/CNPJ"] or "").strip()
                    tels = [r["Telefone 1"],r["Telefone 2"],r["Telefone 3"]]

                    if not nome:
                        invalidos += 1
                        continue

                    if eh_duplicado(doc, tels, carregar_empresas()):
                        ignorados += 1
                        continue

                    # No lote, documento pode estar ausente. Se existir, precisa ter 11 ou 14 dígitos.
                    if doc and len(somente_digitos(doc)) not in (11,14):
                        invalidos += 1
                        continue

                    salvar_empresa(doc, nome, tels, "SEM CONTATO", "", "IMPORTAÇÃO EM LOTE")
                    incluidos += 1

                st.success(
                    f"{incluidos} contato(s) incluído(s) na fila. "
                    f"{ignorados} duplicado(s) ignorado(s). "
                    f"{invalidos} registro(s) precisaram ser ignorados por falta de dados mínimos."
                )
                st.rerun()

# ---------------- EMPRESAS ----------------
elif menu == "🏢 Consulta / Editar Clientes":
    st.subheader("🏢 Consulta / Editar Clientes")
    st.caption("Esta tela é para consulta e edição direta da carteira. Para trabalhar a sequência diária, use a Fila de contatos.")
    busca = st.text_input(
        "Pesquisar por nome, CPF/CNPJ ou telefone",
        placeholder="Comece a digitar o nome da empresa..."
    )
    status_filtro = st.multiselect(
        "Filtrar por status",
        sorted(empresas["status"].dropna().unique().tolist())
    )

    view = empresas.copy()

    if busca:
        termo = busca.strip().lower()

        # Nome começando pelo texto digitado recebe prioridade.
        nome_inicio = view["nome"].fillna("").str.lower().str.startswith(termo)

        # Também mantém busca por nome, documento e telefones.
        campos_busca = ["nome", "documento", "telefone1", "telefone2", "telefone3"]
        mask_geral = pd.Series(False, index=view.index)

        for col in campos_busca:
            mask_geral = mask_geral | view[col].fillna("").astype(str).str.lower().str.contains(
                termo, regex=False
            )

        sugestoes = view[mask_geral].copy()
        sugestoes["_prioridade"] = (~sugestoes.index.isin(view[nome_inicio].index)).astype(int)
        sugestoes = sugestoes.sort_values(["_prioridade", "nome"]).head(15)

        if not sugestoes.empty:
            opcoes_sugestao = ["— Mostrar todos os resultados —"]
            mapa_sugestao = {}

            for _, r in sugestoes.iterrows():
                complemento = r["documento"] or r["telefone1"] or ""
                rotulo = f"{r['nome']} — {complemento}" if complemento else r["nome"]
                opcoes_sugestao.append(rotulo)
                mapa_sugestao[rotulo] = int(r["id"])

            selecionado = st.selectbox(
                "Sugestões",
                opcoes_sugestao,
                key="empresa_sugestao_busca"
            )

            if selecionado != "— Mostrar todos os resultados —":
                empresa_id_sel = mapa_sugestao[selecionado]
                view = view[view["id"] == empresa_id_sel]
            else:
                view = sugestoes.drop(columns=["_prioridade"])
        else:
            view = view.iloc[0:0]

    if status_filtro:
        view = view[view["status"].isin(status_filtro)]

    st.caption(f"{len(view)} registro(s)")
    editor_empresas(view, key_prefix="consulta_clientes")

# ---------------- NOVA EMPRESA ----------------
elif menu == "➕ Nova Empresa":
    st.subheader("➕ Nova Empresa / Cliente")
    with st.form("nova_empresa_v3"):
        documento = st.text_input(
            "CPF ou CNPJ *",
            placeholder="000.000.000-00 ou 00.000.000/0000-00"
        )
        nome = st.text_input("Nome da empresa / cliente *")

        c1,c2,c3 = st.columns(3)
        t1 = c1.text_input("Telefone 1", placeholder="(00) 00000-0000")
        t2 = c2.text_input("Telefone 2", placeholder="(00) 00000-0000")
        t3 = c3.text_input("Telefone 3", placeholder="(00) 00000-0000")

        obs = st.text_area("Observação")
        salvar = st.form_submit_button("Salvar cadastro", type="primary")

    if salvar:
        erros = []
        if not nome.strip():
            erros.append("Informe o nome da empresa/cliente.")
        if not documento_valido(documento):
            erros.append("Informe um CPF ou CNPJ válido.")

        d = somente_digitos(documento)
        if not empresas.empty and empresas["documento"].fillna("").map(somente_digitos).eq(d).any():
            erros.append("Este CPF/CNPJ já está cadastrado.")

        for rotulo, tel in [("Telefone 1",t1),("Telefone 2",t2),("Telefone 3",t3)]:
            if tel and len(somente_digitos(tel)) not in (10,11):
                erros.append(f"{rotulo} deve ter DDD e 10 ou 11 dígitos.")

        if erros:
            for e in erros:
                st.error(e)
        else:
            salvar_empresa(documento, nome, [t1,t2,t3], "SEM CONTATO", obs)
            st.success("Empresa/cliente cadastrada e adicionada à fila de novos contatos.")
            st.rerun()

# ---------------- RELATÓRIOS ----------------
elif menu == "📈 Relatórios":
    st.subheader("📈 Relatórios")
    st.caption("Visão por período e exportação completa da base.")

    c1,c2 = st.columns(2)
    inicio = c1.date_input(
        "De",
        value=date.today()-timedelta(days=30),
        format="DD/MM/YYYY"
    )
    fim = c2.date_input(
        "Até",
        value=date.today(),
        format="DD/MM/YYYY"
    )

    if inicio > fim:
        st.error("A data inicial não pode ser maior que a data final.")
    else:
        rel = contatos.copy()
        if not rel.empty:
            rel["data_dt"] = pd.to_datetime(rel["data_contato"], errors="coerce").dt.date
            rel = rel[(rel["data_dt"] >= inicio) & (rel["data_dt"] <= fim)]

        a,b,c,d = st.columns(4)
        a.metric("Contatos", len(rel))
        b.metric("Empresas diferentes", rel["empresa_id"].nunique() if not rel.empty else 0)
        c.metric("Reuniões", int((rel["resultado"]=="REUNIÃO AGENDADA").sum()) if not rel.empty else 0)
        d.metric("Fechamentos", int((rel["resultado"]=="FECHADO").sum()) if not rel.empty else 0)

        st.subheader("Produtividade diária")
        grafico_contatos_dia(rel)

        if not rel.empty:
            st.subheader("Detalhamento editável")
            editor_contatos(rel, key_prefix="relatorio_contatos")

        st.divider()
        st.subheader("Exportação completa")
        st.write(
            "O arquivo contém **Carteira atual**, **Histórico de contatos**, "
            "**Pendências e retornos** e **Resumo**."
        )
        excel = gerar_excel_completo(empresas, contatos)
        st.download_button(
            "⬇️ Baixar relatório completo em Excel",
            excel,
            file_name=f"relatorio_comercial_completo_{date.today().strftime('%d-%m-%Y')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary"
        )

st.sidebar.divider()
st.sidebar.markdown("**Base de dados**")

if github_ativo():
    st.sidebar.success("☁️ GitHub persistente conectado")
else:
    st.sidebar.error("❌ GitHub NÃO conectado")

if st.sidebar.button("🔄 Carregar base de dados", use_container_width=True):
    try:
        carregar_database(forcar_github=True)
        st.sidebar.success("Base oficial recarregada do GitHub.")
        st.rerun()
    except Exception as e:
        st.sidebar.error(f"Falha ao carregar: {e}")

st.sidebar.caption("Gestão Comercial • PERSISTENTE V5")

