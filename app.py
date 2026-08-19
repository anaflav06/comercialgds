
import streamlit as st
import pandas as pd
import re
import io
import json
import base64
import requests
import time
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

def github_baixar_arquivo_raw(path_repo=None):
    """Baixa bytes crus do GitHub com tentativas automáticas em falhas temporárias."""
    if not github_ativo():
        raise RuntimeError("GitHub não configurado.")

    _, repo, branch, db_path = github_config()
    path_repo = path_repo or db_path
    headers = github_headers().copy()
    headers["Accept"] = "application/vnd.github.raw+json"

    ultimo_status = None
    for tentativa in range(4):
        try:
            r = requests.get(
                f"https://api.github.com/repos/{repo}/contents/{path_repo}",
                headers=headers,
                params={"ref": branch},
                timeout=60,
            )
            ultimo_status = r.status_code
            if r.status_code == 404:
                return None
            if r.status_code == 200:
                return r.content
            if r.status_code in (403, 408, 409, 429, 500, 502, 503, 504):
                time.sleep(1.5 * (tentativa + 1))
                continue
            raise RuntimeError(
                f"Não foi possível baixar {path_repo} do GitHub ({r.status_code})."
            )
        except requests.RequestException:
            if tentativa == 3:
                raise RuntimeError("Falha temporária de comunicação com o GitHub.")
            time.sleep(1.5 * (tentativa + 1))

    raise RuntimeError(
        f"Não foi possível baixar {path_repo} do GitHub após novas tentativas "
        f"(status {ultimo_status})."
    )


def github_baixar_database(forcar=False):
    info = github_file_info()
    if not info:
        return False

    sha_remoto = info.get("sha")
    sha_local = st.session_state.get("_github_db_sha")

    if (
        not forcar
        and DATABASE_PATH.exists()
        and sha_remoto
        and sha_local == sha_remoto
    ):
        st.session_state["_database_remota_carregada"] = True
        return True

    dados_bytes = github_baixar_arquivo_raw()
    if dados_bytes is None:
        return False

    try:
        dados = json.loads(dados_bytes.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(
            "O database.json salvo no GitHub está inválido ou não pôde ser lido."
        ) from exc

    dados.setdefault("metadata", {})
    dados.setdefault("empresas", [])
    dados.setdefault("contatos", [])
    dados.setdefault("acoes_base", [])

    tmp = DATABASE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    tmp.replace(DATABASE_PATH)

    st.session_state["_database_remota_carregada"] = True
    st.session_state["_github_db_sha"] = sha_remoto
    return True


def github_criar_backup_do_atual():
    """Cria um snapshot diário da base anterior, inclusive quando o JSON passa de 1 MB."""
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

    dados_bytes = github_baixar_arquivo_raw(db_path)
    if dados_bytes is None:
        return

    payload = {
        "message": f"Backup database {date.today().strftime('%d/%m/%Y')}",
        "content": base64.b64encode(dados_bytes).decode("ascii"),
        "branch": branch,
    }

    r = requests.put(
        f"https://api.github.com/repos/{repo}/contents/{backup_path}",
        headers=github_headers(),
        json=payload,
        timeout=60,
    )

    if r.status_code not in (200, 201):
        raise RuntimeError(
            f"Não foi possível criar o backup diário ({r.status_code})."
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

    try:
        st.session_state["_github_db_sha"] = r.json()["content"]["sha"]
    except Exception:
        pass
    st.session_state["_database_remota_carregada"] = True

def salvar_database(dados):
    """
    A base oficial é o database.json da branch 'database'.
    Primeiro grava no GitHub; só depois atualiza a cópia local.
    """
    dados.setdefault("metadata", {})
    dados.setdefault("empresas", [])
    dados.setdefault("contatos", [])
    dados.setdefault("acoes_base", [])
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
        existe_remota = github_baixar_database(forcar=forcar_github)
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
                    "acoes_base": [],
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
    dados.setdefault("acoes_base", [])
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
    "SOLICITOU COTAÇÃO",
    "COTAÇÃO ENVIADA",
    "PROPOSTA ENVIADA",
    "EM NEGOCIAÇÃO",
    "FECHADO",
    "SEM INTERESSE",
    "NÃO UTILIZA TRANSPORTE",
    "JÁ UTILIZA AZUL",
    "CONTATO INVÁLIDO",
    "SEM TELEFONE NA BASE",
    "AGUARDANDO CONTATO DO RESPONSÁVEL",
    "SEM TELEFONE NA BASE",
    "AGUARDANDO CONTATO DO RESPONSÁVEL",
    "CONTATO PESSOA FÍSICA / INCORRETO",
    "SEM CONTATO LOCALIZADO",
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
    "AGUARDANDO CONTATO DO RESPONSÁVEL",
    "REUNIÃO AGENDADA",
    "COTAÇÃO SOLICITADA",
    "COTAÇÃO ENVIADA",
    "PROPOSTA SOLICITADA",
    "PROPOSTA ENVIADA",
    "EM NEGOCIAÇÃO",
]

STATUS_ENCERRADOS = [
    "FECHADO / GANHO",
    "SEM INTERESSE",
    "NÃO UTILIZA TRANSPORTE",
    "JÁ UTILIZA AZUL",
    "CONTATO INVÁLIDO",
    "CONTATO PESSOA FÍSICA / INCORRETO",
    "SEM CONTATO LOCALIZADO",
    "SEM TELEFONE NA BASE",
    "SEM RETORNO APÓS 3 TENTATIVAS",
]


STATUS_EM_ANDAMENTO = {
    "AGUARDANDO CLIENTE",
    "AGUARDANDO CONTATO DO RESPONSÁVEL",
    "RETORNO AGENDADO",
    "EM ANDAMENTO",
    "REUNIÃO AGENDADA",
    "COTAÇÃO SOLICITADA",
    "COTAÇÃO ENVIADA",
    "PROPOSTA SOLICITADA",
    "PROPOSTA ENVIADA",
    "EM NEGOCIAÇÃO",
}

STATUS_FILA_INICIAL = {
    "SEM CONTATO",
    "1ª TENTATIVA SEM RETORNO",
    "2ª TENTATIVA SEM RETORNO",
    "SEM RETORNO",
    "SEM SUCESSO NO CONTATO",
    "TENTATIVA DE CONTATO",
}

STATUS_RETORNO_IMPORTADO = {
    "AGUARDANDO CLIENTE",
    "SEM RETORNO",
    "SEM SUCESSO NO CONTATO",
    "TENTATIVA DE CONTATO",
    "1ª TENTATIVA SEM RETORNO",
    "2ª TENTATIVA SEM RETORNO",
    "EM ANDAMENTO",
    "AGUARDANDO CONTATO DO RESPONSÁVEL",
    "REUNIÃO AGENDADA",
    "COTAÇÃO SOLICITADA",
    "COTAÇÃO ENVIADA",
    "PROPOSTA SOLICITADA",
    "PROPOSTA ENVIADA",
    "EM NEGOCIAÇÃO",
}


RESULTADOS_AGUARDANDO = {
    "NÃO CONSEGUI CONTATO",
    "CLIENTE RESPONDEU",
    "AGUARDANDO CLIENTE",
    "RETORNAR EM OUTRA DATA",
    "AGUARDANDO CONTATO DO RESPONSÁVEL",
    "REUNIÃO AGENDADA",
    "SOLICITOU COTAÇÃO",
    "COTAÇÃO ENVIADA",
    "PROPOSTA ENVIADA",
    "EM NEGOCIAÇÃO",
    "OUTRO",
}

MAPA_STATUS = {
    "CLIENTE RESPONDEU": "EM ANDAMENTO",
    "AGUARDANDO CLIENTE": "AGUARDANDO CLIENTE",
    "RETORNAR EM OUTRA DATA": "RETORNO AGENDADO",
    "REUNIÃO AGENDADA": "REUNIÃO AGENDADA",
    "SOLICITOU COTAÇÃO": "COTAÇÃO SOLICITADA",
    "COTAÇÃO ENVIADA": "COTAÇÃO ENVIADA",
    "PROPOSTA ENVIADA": "PROPOSTA ENVIADA",
    "EM NEGOCIAÇÃO": "EM NEGOCIAÇÃO",
    "FECHADO": "FECHADO / GANHO",
    "SEM INTERESSE": "SEM INTERESSE",
    "NÃO UTILIZA TRANSPORTE": "NÃO UTILIZA TRANSPORTE",
    "JÁ UTILIZA AZUL": "JÁ UTILIZA AZUL",
    "CONTATO INVÁLIDO": "CONTATO INVÁLIDO",
    "SEM TELEFONE NA BASE": "SEM TELEFONE NA BASE",
    "AGUARDANDO CONTATO DO RESPONSÁVEL": "AGUARDANDO CONTATO DO RESPONSÁVEL",
                "CONTATO PESSOA FÍSICA / INCORRETO": "CONTATO PESSOA FÍSICA / INCORRETO",
                "SEM CONTATO LOCALIZADO": "SEM CONTATO LOCALIZADO",
    "CONTATO PESSOA FÍSICA / INCORRETO": "CONTATO PESSOA FÍSICA / INCORRETO",
    "SEM CONTATO LOCALIZADO": "SEM CONTATO LOCALIZADO",
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
            "contatos": [],
            "acoes_base": []
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

def email_valido(valor):
    valor = str(valor or "").strip().lower()
    if not valor:
        return False
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", valor))

def normalizar_email(valor):
    valor = str(valor or "").strip().lower()
    return valor if email_valido(valor) else ""

def tem_identificador_util(documento="", telefones=None, email=""):
    telefones = telefones or []
    doc = somente_digitos(documento)
    tem_doc = len(doc) in (11, 14)
    tem_tel = any(len(somente_digitos(t)) in (10, 11) for t in telefones)
    tem_email = email_valido(email)
    return tem_doc or tem_tel or tem_email

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
    # Nunca reimporta/reescreve uma base que já possui clientes.
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


def limpar_cadastros_sem_identificador():
    """
    Limpeza única solicitada pela gestão.
    Remove registros que possuem somente nome, sem CPF/CNPJ, telefone válido ou e-mail.
    Também remove históricos órfãos ligados exclusivamente a esses registros.
    """
    dados = carregar_database(forcar_github=False)
    meta = dados.setdefault("metadata", {})

    if meta.get("cleanup_nome_sem_identificador_v1"):
        return int(meta.get("cleanup_nome_sem_identificador_v1_removidos", 0) or 0)

    remover_ids = set()
    for emp in dados.get("empresas", []):
        if not tem_identificador_util(
            emp.get("documento", ""),
            [emp.get("telefone1", ""), emp.get("telefone2", ""), emp.get("telefone3", "")],
            emp.get("email", "")
        ):
            remover_ids.add(int(emp.get("id", 0) or 0))

    if remover_ids:
        dados["empresas"] = [
            e for e in dados.get("empresas", [])
            if int(e.get("id", 0) or 0) not in remover_ids
        ]
        dados["contatos"] = [
            c for c in dados.get("contatos", [])
            if int(c.get("empresa_id", 0) or 0) not in remover_ids
        ]
        dados["acoes_base"] = [
            a for a in dados.get("acoes_base", [])
            if int(a.get("empresa_id", 0) or 0) not in remover_ids
        ]

    meta["cleanup_nome_sem_identificador_v1"] = True
    meta["cleanup_nome_sem_identificador_v1_removidos"] = len(remover_ids)
    meta["cleanup_nome_sem_identificador_v1_data"] = datetime.now().isoformat(timespec="seconds")
    salvar_database(dados)
    return len(remover_ids)

# -----------------------------
# DADOS
# -----------------------------
def carregar_empresas():
    dados = carregar_database()
    df = pd.DataFrame(dados["empresas"])
    if df.empty:
        return pd.DataFrame(columns=[
            "id","documento","nome","email","telefone1","telefone2","telefone3","status",
            "observacao_atual","data_primeiro_contato","criado_em","origem",
            "retorno_apos_seq","data_agendamento","agendamento_pendente","proxima_acao"
        ])
    for col in [
        "documento","nome","email","telefone1","telefone2","telefone3","status",
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
            "nome","documento","email","telefone1","telefone2","telefone3"
        ])

    for col in [
        "id","empresa_id","data_contato","tipo_contato","resultado","status_novo",
        "observacao","proxima_acao","data_proxima_acao","criado_em","seq_global"
    ]:
        if col not in contatos_df.columns:
            contatos_df[col] = None

    if not empresas_df.empty:
        contatos_df = contatos_df.merge(
            empresas_df[["id","nome","documento","email","telefone1","telefone2","telefone3"]],
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

def salvar_empresa(documento, nome, telefones, status="SEM CONTATO", obs="", origem="APP", email=""):
    dados = carregar_database(forcar_github=True)
    empresa_id = proximo_id(dados["empresas"])
    dados["empresas"].append({
        "id": empresa_id,
        "documento": formatar_documento(documento),
        "nome": nome.strip().upper(),
        "email": normalizar_email(email),
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


def salvar_empresas_em_lote(registros):
    """
    Importação em lote segura:
    - nome é obrigatório;
    - além do nome, precisa ter pelo menos CPF/CNPJ, telefone ou e-mail;
    - todo o lote é salvo em UMA única gravação persistente.
    """
    dados = carregar_database(forcar_github=True)

    docs_existentes = {
        somente_digitos(e.get("documento", ""))
        for e in dados["empresas"]
        if somente_digitos(e.get("documento", ""))
    }
    emails_existentes = {
        str(e.get("email", "") or "").strip().lower()
        for e in dados["empresas"]
        if email_valido(e.get("email", ""))
    }
    tels_existentes = set()
    for e in dados["empresas"]:
        for campo in ("telefone1", "telefone2", "telefone3"):
            tel = somente_digitos(e.get(campo, ""))
            if len(tel) in (10, 11):
                tels_existentes.add(tel)

    proximo = proximo_id(dados["empresas"])
    incluidos = duplicados = invalidos = sem_identificador = 0
    agora = datetime.now().isoformat(timespec="seconds")

    for registro in registros:
        nome = str(registro.get("nome") or "").strip()
        doc = str(registro.get("documento") or "").strip()
        email = normalizar_email(registro.get("email", ""))
        telefones = list(registro.get("telefones") or ["", "", ""])
        while len(telefones) < 3:
            telefones.append("")

        if not nome:
            invalidos += 1
            continue

        doc_dig = somente_digitos(doc)
        if doc and len(doc_dig) not in (11, 14):
            invalidos += 1
            continue

        tels_dig = [
            somente_digitos(t) for t in telefones
            if len(somente_digitos(t)) in (10, 11)
        ]

        if not tem_identificador_util(doc, telefones, email):
            sem_identificador += 1
            continue

        if (
            (doc_dig and doc_dig in docs_existentes)
            or (email and email in emails_existentes)
            or any(t in tels_existentes for t in tels_dig)
        ):
            duplicados += 1
            continue

        dados["empresas"].append({
            "id": proximo,
            "documento": formatar_documento(doc),
            "nome": nome.upper(),
            "email": email,
            "telefone1": formatar_telefone(telefones[0]),
            "telefone2": formatar_telefone(telefones[1]),
            "telefone3": formatar_telefone(telefones[2]),
            "status": "SEM CONTATO",
            "observacao_atual": "",
            "data_primeiro_contato": None,
            "criado_em": agora,
            "origem": "IMPORTAÇÃO EM LOTE",
            "retorno_apos_seq": None,
            "data_agendamento": None,
            "agendamento_pendente": 0,
            "proxima_acao": ""
        })

        if doc_dig:
            docs_existentes.add(doc_dig)
        if email:
            emails_existentes.add(email)
        for t in tels_dig:
            tels_existentes.add(t)

        proximo += 1
        incluidos += 1

    if incluidos > 0:
        salvar_database(dados)

    return incluidos, duplicados, invalidos, sem_identificador


def registrar_acao_base(empresa_id, acao, detalhes=""):
    """Registra esforço de qualificação da carteira sem contar como contato comercial."""
    dados = carregar_database(forcar_github=True)
    dados.setdefault("acoes_base", [])

    agora = datetime.now().isoformat(timespec="seconds")

    # Evita clique duplicado da mesma ação para o mesmo cliente em poucos segundos.
    for item in reversed(dados["acoes_base"][-20:]):
        if (
            int(item.get("empresa_id", 0) or 0) == int(empresa_id)
            and item.get("acao") == acao
            and item.get("usuario") == st.session_state.get("usuario_logado", "")
        ):
            try:
                dt = datetime.fromisoformat(item.get("criado_em"))
                if (datetime.now() - dt).total_seconds() < 15:
                    return False
            except Exception:
                pass

    dados["acoes_base"].append({
        "id": proximo_id(dados["acoes_base"]),
        "empresa_id": int(empresa_id),
        "acao": acao,
        "detalhes": str(detalhes or "").strip(),
        "data": date.today().isoformat(),
        "criado_em": agora,
        "usuario": st.session_state.get("usuario_logado", ""),
    })

    salvar_database(dados)
    return True


def salvar_contato_externo_encontrado(empresa_id, telefone, fonte=""):
    """Adiciona o novo telefone encontrado ao primeiro campo livre e registra recuperação da base."""
    telefone_fmt = formatar_telefone(telefone)
    dig = somente_digitos(telefone)

    if len(dig) not in (10, 11):
        raise ValueError("Informe um telefone válido com DDD.")

    dados = carregar_database(forcar_github=True)
    dados.setdefault("acoes_base", [])

    empresa = next(
        (e for e in dados["empresas"] if int(e.get("id", 0) or 0) == int(empresa_id)),
        None
    )
    if not empresa:
        raise ValueError("Cliente não encontrado.")

    existentes = [
        somente_digitos(empresa.get("telefone1", "")),
        somente_digitos(empresa.get("telefone2", "")),
        somente_digitos(empresa.get("telefone3", "")),
    ]
    if dig in existentes:
        raise ValueError("Esse telefone já está cadastrado para este cliente.")

    campo_salvo = None
    for campo in ("telefone1", "telefone2", "telefone3"):
        atual = str(empresa.get(campo, "") or "").strip()
        if not atual or atual.upper() in ("NÃO TEM", "NAO TEM", "NAN", "NONE", "-"):
            empresa[campo] = telefone_fmt
            campo_salvo = campo
            break

    if not campo_salvo:
        raise ValueError(
            "Os três campos de telefone já estão preenchidos. "
            "Use 'Editar dados deste cliente' para substituir algum telefone."
        )

    # Se estava marcado como problema de base, volta para acompanhamento.
    if str(empresa.get("status", "")).upper() in {
        "SEM TELEFONE NA BASE",
        "CONTATO INVÁLIDO",
        "CONTATO PESSOA FÍSICA / INCORRETO",
        "SEM CONTATO LOCALIZADO",
    }:
        empresa["status"] = "SEM CONTATO"
        empresa["retorno_apos_seq"] = None
        empresa["agendamento_pendente"] = 0
        empresa["data_agendamento"] = None

    agora = datetime.now().isoformat(timespec="seconds")
    dados["acoes_base"].append({
        "id": proximo_id(dados["acoes_base"]),
        "empresa_id": int(empresa_id),
        "acao": "CONTATO EXTERNO ENCONTRADO",
        "detalhes": f"{telefone_fmt}" + (f" | Fonte: {fonte.strip()}" if fonte.strip() else ""),
        "data": date.today().isoformat(),
        "criado_em": agora,
        "usuario": st.session_state.get("usuario_logado", ""),
    })

    salvar_database(dados)
    return telefone_fmt


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
            status_novo = "SEM RETORNO APÓS 3 TENTATIVAS"
    else:
        status_novo = MAPA_STATUS.get(resultado, "EM ANDAMENTO")

    retorno_apos = None
    agendamento_pendente = 0
    data_agendamento_iso = None

    # Status finalizadores nunca retornam para a fila.
    if status_novo in STATUS_ENCERRADOS:
        data_agendamento = None

    # Data específica só vale para status que continuam ativos.
    if data_agendamento and status_novo not in STATUS_ENCERRADOS:
        data_agendamento_iso = data_agendamento.isoformat()
        agendamento_pendente = 1
        status_novo = "RETORNO AGENDADO"

    # Sem data específica, todo status ativo volta após 200 contatos.
    elif status_novo not in STATUS_ENCERRADOS:
        retorno_apos = seq + 200

    # Finalizadores limpam qualquer pendência.
    else:
        retorno_apos = None
        data_agendamento_iso = None
        agendamento_pendente = 0

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
                      status, observacao, proxima_acao, data_agendamento=None, email=""):
    """Salva qualquer edição do cadastro e sincroniza imediatamente no database.json."""
    dados = carregar_database(forcar_github=True)
    encontrado = False

    for emp in dados["empresas"]:
        if int(emp.get("id", 0) or 0) == int(empresa_id):
            emp["nome"] = str(nome or "").strip().upper()
            emp["documento"] = formatar_documento(documento)
            emp["email"] = normalizar_email(email)
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
        emp["email"] = normalizar_email(row.get("E-mail", ""))
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
        "id","documento","nome","email","telefone1","telefone2","telefone3",
        "status","observacao_atual","data_primeiro_contato","proxima_acao","data_agendamento"
    ]].copy()

    tabela["data_primeiro_contato"] = tabela["data_primeiro_contato"].apply(data_br)
    tabela["data_agendamento"] = tabela["data_agendamento"].apply(data_br)
    tabela.columns = [
        "ID","CPF/CNPJ","Empresa / Cliente","E-mail","Telefone 1","Telefone 2","Telefone 3",
        "Status","Última observação","1º contato","Próxima ação","Agendamento"
    ]

    status_opcoes = [
        "SEM CONTATO","1ª TENTATIVA SEM RETORNO","2ª TENTATIVA SEM RETORNO",
        "SEM RETORNO APÓS 3 TENTATIVAS",
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



def pular_cliente_por_enquanto(empresa_id):
    """Pula sem contar tentativa nem contato. Retorna após 200 contatos reais."""
    dados = carregar_database(forcar_github=True)
    seq_atual = max(
        [int(c.get("seq_global") or 0) for c in dados.get("contatos", []) if c.get("seq_global") is not None]
        or [0]
    )
    for emp in dados["empresas"]:
        if int(emp.get("id", 0) or 0) == int(empresa_id):
            emp["retorno_apos_seq"] = seq_atual + 200
            emp["agendamento_pendente"] = 0
            emp["data_agendamento"] = None
            emp["proxima_acao"] = "PULADO POR ENQUANTO"
            break
    salvar_database(dados)

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
EMAIL_REGEX = re.compile(r'[\w.\-+]+@[\w.\-]+\.[A-Za-z]{2,}')
PHONE_REGEX = re.compile(
    r'(?:\(?\d{2}\)?[\s\-]*)?(?:9?\d{4})[\s\-]?\d{4}'
)

def extrair_telefones(texto):
    encontrados = []
    for m in PHONE_REGEX.findall(texto):
        d = somente_digitos(m)
        if len(d) in (10, 11):
            fmt = formatar_telefone(d)
            if fmt not in encontrados:
                encontrados.append(fmt)
    return encontrados[:3]

def limpar_nome_linha(linha):
    s = linha
    for d in DOC_REGEX.findall(s):
        s = s.replace(d, " ")
    for em in EMAIL_REGEX.findall(s):
        s = s.replace(em, " ")
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
        emails = EMAIL_REGEX.findall(linha)
        telefones = extrair_telefones(linha)
        nome = limpar_nome_linha(linha)

        inicia_novo = bool(docs or emails or telefones)

        if inicia_novo:
            if atual:
                registros.append(atual)
            atual = {
                "documento": docs[0] if docs else "",
                "email": emails[0].lower() if emails else "",
                "nome": nome,
                "telefones": telefones[:],
            }
        else:
            if atual and telefones:
                for tel in telefones:
                    if tel not in atual["telefones"] and len(atual["telefones"]) < 3:
                        atual["telefones"].append(tel)
            elif atual and emails and not atual.get("email"):
                atual["email"] = emails[0].lower()
            elif atual and nome and not atual["nome"]:
                atual["nome"] = nome
            elif nome:
                # Mantém na prévia para sinalizar que será ignorado,
                # mas nunca será importado sem identificador.
                if atual:
                    registros.append(atual)
                atual = {"documento": "", "email": "", "nome": nome, "telefones": []}

    if atual:
        registros.append(atual)

    saida = []
    for r in registros:
        saida.append({
            "CPF/CNPJ": formatar_documento(r.get("documento", "")) if r.get("documento") else "",
            "Nome": str(r.get("nome", "") or "").upper().strip(),
            "E-mail": normalizar_email(r.get("email", "")),
            "Telefone 1": r["telefones"][0] if len(r.get("telefones", [])) > 0 else "",
            "Telefone 2": r["telefones"][1] if len(r.get("telefones", [])) > 1 else "",
            "Telefone 3": r["telefones"][2] if len(r.get("telefones", [])) > 2 else "",
        })

    return pd.DataFrame(saida)

def eh_duplicado(documento, telefones, empresas, email=""):
    doc = somente_digitos(documento)
    if doc:
        docs = empresas["documento"].fillna("").map(somente_digitos)
        if docs.eq(doc).any():
            return True

    email_novo = normalizar_email(email)
    if email_novo and "email" in empresas.columns:
        if empresas["email"].fillna("").str.lower().eq(email_novo).any():
            return True

    tels_novos = {somente_digitos(t) for t in telefones if len(somente_digitos(t)) in (10, 11)}
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
        c1, c2, c3 = st.columns([1.4, 1, 1.2])
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
        email = c3.text_input(
            "E-mail",
            value=str(empresa.get("email") or ""),
            key=f"{prefixo}_{empresa_id}_email"
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
                status, observacao, proxima_acao, data_ag, email
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
        "id","documento","nome","email","telefone1","telefone2","telefone3","status",
        "observacao_atual","Data 1º contato","proxima_acao","Data agendamento",
        "agendamento_pendente","retorno_apos_seq","origem","criado_em"
    ]].copy()
    carteira_export.columns = [
        "ID","CPF/CNPJ","Empresa / Cliente","E-mail","Telefone 1","Telefone 2","Telefone 3",
        "Status atual","Última observação","Data 1º contato","Próxima ação",
        "Data agendada","Agendamento pendente","Retorno após sequência",
        "Origem","Criado em"
    ]

    hist = contatos.copy()
    if not hist.empty:
        hist["Data contato"] = hist["data_contato"].apply(data_br)
        hist["Data retorno"] = hist["data_proxima_acao"].apply(data_br)
        hist_export = hist[[
            "id","empresa_id","nome","documento","email","telefone1","telefone2","telefone3",
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
# REGRA DE OURO DA PERSISTÊNCIA:
# - abrir/reiniciar/deployar o app NÃO pode alterar database.json;
# - cada gravação baixa a versão mais recente do GitHub antes de modificar;
# - somente uma ação explícita do usuário pode gerar mudança na base.

# Sincroniza a base oficial apenas na abertura da sessão.
if github_ativo():
    try:
        carregar_database(forcar_github=False)
        st.session_state["_github_online"] = True
    except Exception as e:
        st.session_state["_github_online"] = False
        st.session_state["_github_erro"] = str(e)
        if not DATABASE_PATH.exists():
            raise

criar_banco()
importar_planilha_inicial()

# IMPORTANTE: nenhuma limpeza automática é executada na abertura.
# A base persistente nunca deve ser alterada apenas por abrir/reiniciar/deployar o app.
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
        "🔥 Clientes em andamento",
        "➕ Adicionar contatos em lote",
        "🏢 Consulta / Editar Clientes",
        "➕ Nova Empresa",
        "📈 Relatórios",
    ]
)

# ---------------- DASHBOARD ----------------

def normalizar_data_historico(valor):
    """Aceita ISO, dd/mm/aaaa, timestamps e datas antigas sem perder registros válidos."""
    if valor is None:
        return pd.NaT

    texto = str(valor).strip()
    if not texto or texto.lower() in {"nan", "none", "nat"}:
        return pd.NaT

    # ISO / timestamp
    dt = pd.to_datetime(texto, errors="coerce")
    if pd.notna(dt):
        return dt

    # Formato brasileiro explícito
    dt = pd.to_datetime(texto, errors="coerce", dayfirst=True)
    return dt


if menu == "📊 Dashboard":
    hoje = date.today()
    st.markdown("""
    <style>
    .dash-title{font-size:2rem;font-weight:800;color:#20263a;margin-bottom:.1rem}
    .dash-sub{color:#6b7280;margin-bottom:1rem}
    .kpi{background:white;border:1px solid #e7eaf0;border-radius:16px;padding:16px;
         box-shadow:0 3px 12px rgba(30,41,59,.06);min-height:116px}
    .kpi-t{font-size:.8rem;color:#6b7280;font-weight:650}
    .kpi-v{font-size:1.9rem;color:#1f2937;font-weight:800;margin:.3rem 0}
    .kpi-f{font-size:.76rem;color:#8a91a3}
    .sec{font-size:1.3rem;font-weight:780;color:#20263a;margin-top:.5rem}
    .note{font-size:.86rem;color:#7d8495;margin-bottom:.6rem}
    </style>
    """, unsafe_allow_html=True)

    st.markdown('<div class="dash-title">📊 Dashboard Comercial</div>', unsafe_allow_html=True)
    st.markdown('<div class="dash-sub">Análise objetiva da produtividade, contato efetivo e conversão comercial.</div>', unsafe_allow_html=True)

    analitico = contatos.copy()
    if not analitico.empty:
        # Toda métrica é reconstruída a partir do histórico persistente.
        # Não excluímos registros antigos/importados apenas pelo tipo, pois versões
        # anteriores do app podem ter gravado contatos reais nessa classificação.
        analitico["_data_parse"] = analitico["data_contato"].apply(normalizar_data_historico)
        analitico["data_dt"] = analitico["_data_parse"].apply(
            lambda x: x.date() if pd.notna(x) else None
        )
        analitico = analitico[analitico["data_dt"].notna()].copy()

        def status_g(row):
            s = str(row.get("status_novo", "") or "").strip().upper()
            r = str(row.get("resultado", "") or "").strip().upper()
            if s in {"1ª TENTATIVA SEM RETORNO","2ª TENTATIVA SEM RETORNO","SEM RETORNO APÓS 3 TENTATIVAS",
                     "SEM RETORNO","SEM SUCESSO NO CONTATO","TENTATIVA DE CONTATO"}:
                return "SEM RETORNO"
            return s or r or "SEM CLASSIFICAÇÃO"

        analitico["status_gerencial"] = analitico.apply(status_g, axis=1)

        # Mantém contatos históricos reais, inclusive os originados de versões antigas.
        # Só descarta linhas completamente vazias, sem data/empresa/resultado/status.
        analitico = analitico[
            analitico["data_dt"].notna()
            & analitico["empresa_id"].notna()
        ].copy()

    periodo = st.selectbox(
        "Período",
        ["Hoje","Ontem","Últimos 7 dias","Últimos 15 dias","Últimos 30 dias",
         "Este mês","Mês anterior","Período personalizado","Todo histórico"],
        key="dash_periodo_final"
    )

    if periodo == "Hoje":
        inicio = fim = hoje
    elif periodo == "Ontem":
        inicio = fim = hoje - timedelta(days=1)
    elif periodo == "Últimos 7 dias":
        fim = hoje; inicio = fim - timedelta(days=6)
    elif periodo == "Últimos 15 dias":
        fim = hoje; inicio = fim - timedelta(days=14)
    elif periodo == "Últimos 30 dias":
        fim = hoje; inicio = fim - timedelta(days=29)
    elif periodo == "Este mês":
        inicio = hoje.replace(day=1); fim = hoje
    elif periodo == "Mês anterior":
        fim = hoje.replace(day=1) - timedelta(days=1); inicio = fim.replace(day=1)
    elif periodo == "Período personalizado":
        a,b = st.columns(2)
        inicio = a.date_input("De", value=hoje-timedelta(days=14), max_value=hoje, format="DD/MM/YYYY", key="dash_de_final")
        fim = b.date_input("Até", value=hoje, max_value=hoje, format="DD/MM/YYYY", key="dash_ate_final")
        if inicio > fim:
            st.error("A data inicial não pode ser maior que a final.")
            st.stop()
    else:
        if analitico.empty:
            inicio = hoje
        else:
            inicio = analitico["data_dt"].min()
        fim = hoje

    dias_periodo = max(1, (fim-inicio).days+1)
    selecionado = (
        analitico[(analitico["data_dt"]>=inicio)&(analitico["data_dt"]<=fim)].copy()
        if not analitico.empty else pd.DataFrame()
    )

    def kpi(t,v,f=""):
        st.markdown(f'<div class="kpi"><div class="kpi-t">{t}</div><div class="kpi-v">{v}</div><div class="kpi-f">{f}</div></div>', unsafe_allow_html=True)

    total = len(selecionado)
    empresas_periodo = selecionado["empresa_id"].nunique() if not selecionado.empty else 0
    media = round(total/dias_periodo,1)
    status_s = selecionado["status_gerencial"] if not selecionado.empty else pd.Series(dtype=str)
    problemas_base = int(status_s.isin(["CONTATO INVÁLIDO","CONTATO PESSOA FÍSICA / INCORRETO",
                                         "SEM CONTATO LOCALIZADO","SEM TELEFONE NA BASE"]).sum()) if total else 0
    sem_retorno = int((status_s=="SEM RETORNO").sum()) if total else 0
    contatos_efetivos = max(total - sem_retorno - problemas_base, 0)
    avanços = int(status_s.isin(["AGUARDANDO CLIENTE","AGUARDANDO CONTATO DO RESPONSÁVEL","RETORNO AGENDADO",
                                 "EM ANDAMENTO","REUNIÃO AGENDADA","COTAÇÃO SOLICITADA","COTAÇÃO ENVIADA",
                                 "PROPOSTA SOLICITADA","PROPOSTA ENVIADA","EM NEGOCIAÇÃO","FECHADO / GANHO"]).sum()) if total else 0
    fechados = int((status_s=="FECHADO / GANHO").sum()) if total else 0
    taxa_contato = round(contatos_efetivos/max(total-problemas_base,1)*100,1) if total else 0
    taxa_avanco = round(avanços/max(contatos_efetivos,1)*100,1) if contatos_efetivos else 0
    conversao = round(fechados/max(contatos_efetivos,1)*100,1) if contatos_efetivos else 0

    st.markdown('<div class="sec">Desempenho do período</div>', unsafe_allow_html=True)
    st.caption(f"{inicio.strftime('%d/%m/%Y')} a {fim.strftime('%d/%m/%Y')}")
    c1,c2,c3,c4 = st.columns(4)
    with c1: kpi("📞 Contatos realizados", total, "registros comerciais")
    with c2: kpi("🏢 Empresas trabalhadas", empresas_periodo, "clientes diferentes")
    with c3: kpi("📈 Média por dia", f"{media:.1f}", "contatos/dia")
    with c4: kpi("📵 Sem retorno", sem_retorno, "tentativas sem contato")

    c1,c2,c3,c4 = st.columns(4)
    with c1: kpi("💬 Taxa de contato efetivo", f"{taxa_contato:.1f}%", "desconsidera problemas da base")
    with c2: kpi("🚀 Taxa de avanço", f"{taxa_avanco:.1f}%", "avanços ÷ contatos efetivos")
    with c3: kpi("🧹 Problemas de base", problemas_base, "não penalizam resultado comercial")
    with c4: kpi("🎯 Conversão", f"{conversao:.1f}%", "fechados ÷ contatos efetivos")

    # Performance só quando há mais de um dia
    if dias_periodo > 1 and not selecionado.empty:
        st.divider()
        st.markdown('<div class="sec">📈 Performance ao longo do período</div>', unsafe_allow_html=True)
        if periodo == "Todo histórico" or dias_periodo > 60:
            tmp = selecionado.copy()
            tmp["Mes"] = pd.to_datetime(tmp["data_dt"]).dt.to_period("M").astype(str)
            serie = tmp.groupby("Mes").agg(Contatos=("id","count"),Empresas=("empresa_id","nunique")).reset_index()
            serie["Periodo"] = pd.to_datetime(serie["Mes"]+"-01").dt.strftime("%m/%Y")
            xfield = "Periodo:N"
        else:
            serie = selecionado.groupby("data_dt").agg(Contatos=("id","count"),Empresas=("empresa_id","nunique")).reset_index()
            serie["Periodo"] = serie["data_dt"].apply(lambda d:d.strftime("%d/%m"))
            xfield = "Periodo:N"

        basec = alt.Chart(serie).encode(
            x=alt.X(xfield, title=None, sort=None),
            tooltip=["Periodo:N","Contatos:Q","Empresas:Q"]
        )
        bars = basec.mark_bar(color="#4F6EF7",cornerRadiusTopLeft=5,cornerRadiusTopRight=5).encode(y=alt.Y("Contatos:Q",title="Contatos"))
        labels = basec.mark_text(dy=-9,fontWeight="bold",color="#25304a").encode(y="Contatos:Q",text=alt.Text("Contatos:Q",format="d"))
        st.altair_chart((bars+labels).properties(height=310),use_container_width=True)

    # Resultado do período com valores exatos
    st.divider()
    st.markdown('<div class="sec">🎯 Resultado dos contatos no período</div>', unsafe_allow_html=True)
    if selecionado.empty:
        st.info("Nenhum contato encontrado no período selecionado.")
        if not analitico.empty:
            datas_disponiveis = sorted(
                {d for d in analitico["data_dt"].dropna().tolist()},
                reverse=True
            )
            if datas_disponiveis:
                ultimas = ", ".join(d.strftime("%d/%m/%Y") for d in datas_disponiveis[:5])
                st.caption(f"Últimas datas encontradas no histórico: {ultimas}")
    else:
        res = status_s.value_counts().rename_axis("Status").reset_index(name="Quantidade")
        bars = alt.Chart(res).mark_bar(cornerRadiusEnd=6).encode(
            y=alt.Y("Status:N",sort="-x",title=None,axis=alt.Axis(labelLimit=250)),
            x=alt.X("Quantidade:Q",title=None,axis=alt.Axis(tickMinStep=1,format="d")),
            color=alt.Color("Status:N",legend=None,scale=alt.Scale(scheme="tableau20")),
            tooltip=["Status:N","Quantidade:Q"]
        )
        labels = alt.Chart(res).mark_text(align="left",dx=7,fontWeight="bold",color="#25304a").encode(
            y=alt.Y("Status:N",sort="-x"),x="Quantidade:Q",text=alt.Text("Quantidade:Q",format="d")
        )
        st.altair_chart((bars+labels).properties(height=max(250,len(res)*31)),use_container_width=True)

    # Canais usados: exclui ações internas do sistema
    st.divider()
    cc1,cc2 = st.columns([1,1.25])
    with cc1:
        st.markdown('<div class="sec">📲 Canais utilizados</div>', unsafe_allow_html=True)
        canais_src = selecionado[
            ~selecionado["tipo_contato"].fillna("").isin(["FINALIZAÇÃO RÁPIDA","HISTÓRICO IMPORTADO"])
        ].copy() if not selecionado.empty else pd.DataFrame()
        if canais_src.empty:
            st.info("Sem canais registrados.")
        else:
            canais = canais_src["tipo_contato"].replace("","OUTRO").value_counts().rename_axis("Canal").reset_index(name="Quantidade")
            canais["Percentual"]=(canais["Quantidade"]/canais["Quantidade"].sum()*100).round(1)
            canais["Legenda"]=canais.apply(lambda r:f"{r['Canal']} — {int(r['Quantidade'])} ({r['Percentual']:.1f}%)",axis=1)
            donut=alt.Chart(canais).mark_arc(innerRadius=60,outerRadius=100).encode(
                theta="Quantidade:Q",
                color=alt.Color("Legenda:N",legend=alt.Legend(title=None,orient="bottom")),
                tooltip=["Canal:N","Quantidade:Q",alt.Tooltip("Percentual:Q",format=".1f",title="%")]
            ).properties(height=330)
            st.altair_chart(donut,use_container_width=True)

    with cc2:
        st.markdown('<div class="sec">🔻 Funil comercial do período</div>', unsafe_allow_html=True)
        oportunidades=int(status_s.isin(["AGUARDANDO CLIENTE","AGUARDANDO CONTATO DO RESPONSÁVEL","RETORNO AGENDADO",
                                          "EM ANDAMENTO","REUNIÃO AGENDADA","COTAÇÃO SOLICITADA","COTAÇÃO ENVIADA",
                                          "PROPOSTA SOLICITADA","PROPOSTA ENVIADA","EM NEGOCIAÇÃO","FECHADO / GANHO"]).sum()) if total else 0
        cotacoes=int(status_s.isin(["COTAÇÃO SOLICITADA","COTAÇÃO ENVIADA","PROPOSTA SOLICITADA","PROPOSTA ENVIADA"]).sum()) if total else 0
        negociacoes=int((status_s=="EM NEGOCIAÇÃO").sum()) if total else 0
        funil=pd.DataFrame([
            {"Etapa":"Empresas trabalhadas","Quantidade":empresas_periodo,"Ordem":1},
            {"Etapa":"Contatos efetivos","Quantidade":contatos_efetivos,"Ordem":2},
            {"Etapa":"Interesse / oportunidade","Quantidade":oportunidades,"Ordem":3},
            {"Etapa":"Cotação / proposta","Quantidade":cotacoes,"Ordem":4},
            {"Etapa":"Negociação","Quantidade":negociacoes,"Ordem":5},
            {"Etapa":"Fechado / ganho","Quantidade":fechados,"Ordem":6},
        ])
        fb=alt.Chart(funil).mark_bar(cornerRadiusEnd=6).encode(
            y=alt.Y("Etapa:N",sort=alt.EncodingSortField(field="Ordem"),title=None),
            x=alt.X("Quantidade:Q",title=None,axis=alt.Axis(tickMinStep=1,format="d")),
            color=alt.Color("Etapa:N",legend=None,scale=alt.Scale(scheme="blues")),
            tooltip=["Etapa:N","Quantidade:Q"]
        )
        fl=alt.Chart(funil).mark_text(align="left",dx=7,fontWeight="bold",color="#25304a").encode(
            y=alt.Y("Etapa:N",sort=alt.EncodingSortField(field="Ordem")),
            x="Quantidade:Q",text=alt.Text("Quantidade:Q",format="d")
        )
        st.altair_chart((fb+fl).properties(height=330),use_container_width=True)

    # Histórico e carteira somente em visão mensal/ampla
    mostrar_amplo = periodo in {"Este mês","Mês anterior","Todo histórico"}

    if mostrar_amplo:
        st.divider()
        st.markdown('<div class="sec">🗂️ Histórico geral</div>', unsafe_allow_html=True)
        hist_view = selecionado if periodo != "Todo histórico" else analitico
        if hist_view.empty:
            st.info("Sem histórico.")
        else:
            hv=hist_view.copy()
            hv["Mes"]=pd.to_datetime(hv["data_dt"]).dt.strftime("%m/%Y")
            resumo=hv.groupby("Mes").agg(Contatos=("id","count"),Empresas=("empresa_id","nunique")).reset_index()
            st.dataframe(resumo.iloc[::-1],use_container_width=True,hide_index=True)

        st.divider()
        st.markdown('<div class="sec">🏢 Situação atual da carteira</div>', unsafe_allow_html=True)
        st.caption("Fotografia da carteira inteira neste momento.")
        if not empresas.empty:
            sit=empresas["status"].fillna("SEM CLASSIFICAÇÃO").value_counts().rename_axis("Status").reset_index(name="Quantidade")
            sb=alt.Chart(sit).mark_bar(cornerRadiusEnd=6).encode(
                y=alt.Y("Status:N",sort="-x",title=None,axis=alt.Axis(labelLimit=250)),
                x=alt.X("Quantidade:Q",title=None,axis=alt.Axis(tickMinStep=1,format="d")),
                color=alt.Color("Status:N",legend=None,scale=alt.Scale(scheme="tableau20")),
                tooltip=["Status:N","Quantidade:Q"]
            )
            sl=alt.Chart(sit).mark_text(align="left",dx=7,fontWeight="bold").encode(
                y=alt.Y("Status:N",sort="-x"),x="Quantidade:Q",text=alt.Text("Quantidade:Q",format="d")
            )
            st.altair_chart((sb+sl).properties(height=max(300,len(sit)*29)),use_container_width=True)

elif menu == "📞 Fila de contatos":
    st.markdown("## 📞 Fila de contatos")
    st.caption("Prospecção inicial: cliente, contato, resultado e próximo.")

    hoje = date.today()
    hoje_ts = pd.Timestamp(hoje)
    seq_atual = seq_global_atual()
    base = empresas.copy()

    if base.empty:
        st.info("A carteira está vazia.")
    else:
        base["ag_dt"] = pd.to_datetime(base["data_agendamento"],errors="coerce").dt.normalize()
        base["ret_seq"] = pd.to_numeric(base["retorno_apos_seq"],errors="coerce")

        # Somente prospecção inicial. Oportunidades avançadas ficam no menu próprio.
        inicial = base[
            (~base["status"].isin(STATUS_ENCERRADOS)) &
            (~base["status"].isin(STATUS_EM_ANDAMENTO))
        ].copy()

        atrasados = inicial[
            (inicial["agendamento_pendente"].fillna(0)==1)&
            inicial["ag_dt"].notna()&(inicial["ag_dt"]<hoje_ts)
        ].copy()
        hoje_ag = inicial[
            (inicial["agendamento_pendente"].fillna(0)==1)&
            inicial["ag_dt"].notna()&(inicial["ag_dt"]==hoje_ts)
        ].copy()
        novos = inicial[
            (inicial["status"]=="SEM CONTATO")&
            (inicial["agendamento_pendente"].fillna(0)!=1)&
            (inicial["ret_seq"].isna())
        ].copy()
        retornos = inicial[
            inicial["ret_seq"].notna()&(inicial["ret_seq"]<=seq_atual)&
            (inicial["agendamento_pendente"].fillna(0)!=1)
        ].copy()
        legado = inicial[
            inicial["status"].isin(STATUS_RETORNO_IMPORTADO)&
            (inicial["agendamento_pendente"].fillna(0)!=1)&
            (inicial["ret_seq"].isna())
        ].copy()

        atrasados["_p"]=1; hoje_ag["_p"]=2; novos["_p"]=3; retornos["_p"]=4; legado["_p"]=5
        fila_df=pd.concat([atrasados,hoje_ag,novos,retornos,legado],ignore_index=True).drop_duplicates("id")

        if fila_df.empty:
            st.success("Não há clientes aguardando prospecção inicial.")
        else:
            fila_df=fila_df.sort_values(["_p","ag_dt","nome"],na_position="last").reset_index(drop=True)
            atual=fila_df.iloc[0]
            empresa_id=int(atual["id"])
            prefixo=f"fila_simple_{empresa_id}"

            flash=st.session_state.pop("flash_contato",None)
            if flash:
                st.success(flash)

            # Voltar ao contato anterior
            if st.session_state.get("mostrar_ultimo_contato"):
                with st.expander("⬅️ Editar contato anterior",expanded=True):
                    editar_ultimo_contato()
                    if st.button("Fechar",key="fechar_anterior_final"):
                        st.session_state["mostrar_ultimo_contato"]=False
                        st.rerun()

            tag=""
            if int(atual["_p"])==1: tag="🔴 ATRASADO"
            elif int(atual["_p"])==2: tag="🟠 AGENDADO HOJE"
            elif atual["status"]=="SEM CONTATO": tag="🆕 NOVO"
            else: tag="🔄 RETORNO"

            tels=[
                str(t).strip() for t in [atual.get("telefone1"),atual.get("telefone2"),atual.get("telefone3")]
                if str(t or "").strip() and str(t or "").strip().upper() not in {"NAN","NONE","NÃO TEM","NAO TEM","-"}
            ]

            # Cabeçalho compacto: tudo essencial em poucas linhas
            st.markdown(
                f"""
                <div style="padding:.2rem 0 .35rem 0;">
                    <div style="display:flex;align-items:center;gap:.55rem;flex-wrap:wrap;">
                        <span style="font-size:1.5rem;font-weight:800;color:#20263a;">{atual['nome']}</span>
                        <span style="font-size:.82rem;padding:.18rem .5rem;border-radius:.6rem;background:#eef3ff;">{tag}</span>
                    </div>
                    <div style="margin-top:.35rem;color:#586174;font-size:.9rem;">
                        <b>CPF/CNPJ:</b> {atual.get('documento') or '-'}
                        &nbsp;&nbsp;•&nbsp;&nbsp;
                        <b>Status:</b> {atual.get('status') or 'SEM CONTATO'}
                        &nbsp;&nbsp;•&nbsp;&nbsp;
                        <b>Fila:</b> 1 de {len(fila_df)}
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

            contato_txt = " • ".join(tels) if tels else "Sem telefone válido cadastrado"
            email_txt = str(atual.get("email") or "").strip()
            linha_contato = f"📞 {contato_txt}"
            if email_txt:
                linha_contato += f" &nbsp;&nbsp;|&nbsp;&nbsp; ✉️ {email_txt}"

            st.markdown(
                f"""
                <div style="background:#f6f8fb;border:1px solid #e6e9ef;border-radius:10px;
                            padding:.65rem .8rem;margin:.25rem 0 .4rem 0;font-size:.95rem;">
                    {linha_contato}
                </div>
                """,
                unsafe_allow_html=True
            )

            # Histórico compacto
            hist_emp = contatos[
                (contatos["empresa_id"]==empresa_id)
            ].copy() if not contatos.empty else pd.DataFrame()
            if not hist_emp.empty:
                ult=hist_emp.iloc[0]
                st.caption(
                    f"Último contato: {data_br(ult.get('data_contato'))} • "
                    f"{ult.get('tipo_contato') or '-'} • {ult.get('resultado') or '-'}"
                )

            c_hist, c_edit = st.columns([1,1])
            with c_hist:
                if not hist_emp.empty:
                    with st.expander("🕘 Histórico", expanded=False):
                        historico_cliente(contatos,empresa_id)
            with c_edit:
                with st.expander("✏️ Editar cadastro",expanded=False):
                    painel_edicao_empresa(atual,prefixo="fila_cadastro")

            st.divider()

            tipo = st.pills(
                "📲 Tipo de contato",
                ["📞 Ligação","💬 WhatsApp","✉️ E-mail","🔹 Outro"],
                selection_mode="single",
                key=f"{prefixo}_tipo"
            )

            resultado_ui = st.pills(
                "🎯 Resultado",
                [
                    "✅ Falou com cliente",
                    "📵 Não conseguiu contato",
                    "🔥 Cliente interessado",
                    "📄 Cotação / proposta",
                    "🚫 Sem interesse",
                    "📦 Finalização comercial",
                    "⚠️ Problema no contato/base",
                ],
                selection_mode="single",
                key=f"{prefixo}_resultado"
            )

            detalhe = None
            proxima = None
            data_ag = None

            if resultado_ui == "⚠️ Problema no contato/base":
                detalhe = st.pills(
                    "Qual problema?",
                    ["☎️ Contato inválido","👤 Pessoa física / incorreto","📵 Sem telefone","🔍 Sem contato localizado"],
                    selection_mode="single",
                    key=f"{prefixo}_problema"
                )

            elif resultado_ui == "📦 Finalização comercial":
                detalhe = st.pills(
                    "Motivo",
                    ["📦 Não utiliza transporte","✈️ Já utiliza Azul"],
                    selection_mode="single",
                    key=f"{prefixo}_final_comercial"
                )

            elif resultado_ui == "📄 Cotação / proposta":
                detalhe = st.pills(
                    "Etapa",
                    ["🧾 Cotação solicitada","📤 Cotação enviada","📄 Proposta enviada"],
                    selection_mode="single",
                    key=f"{prefixo}_etapa"
                )

            elif resultado_ui == "✅ Falou com cliente":
                proxima = st.pills(
                    "➡️ Próximo passo",
                    ["⏳ Aguardar cliente","👔 Aguardar responsável","📅 Agendar retorno","🔥 Em andamento"],
                    selection_mode="single",
                    key=f"{prefixo}_proxima"
                )
                if proxima == "📅 Agendar retorno":
                    data_ag=st.date_input(
                        "Data do retorno",
                        value=hoje+timedelta(days=1),
                        min_value=hoje,
                        format="DD/MM/YYYY",
                        key=f"{prefixo}_data_ag"
                    )

            obs=st.text_area(
                "📝 Observação (opcional)",
                placeholder="Ex.: responsável pediu retorno na próxima semana.",
                key=f"{prefixo}_obs",
                height=90
            )

            st.divider()
            b1,b2,b3,b4=st.columns([1,1.1,1.7,1.15])

            with b1:
                anterior_bt=st.button("⬅️ Anterior",use_container_width=True,key=f"{prefixo}_anterior")
            with b2:
                pular_bt=st.button("⏭️ Pular",use_container_width=True,key=f"{prefixo}_pular")
            with b3:
                salvar_bt=st.button("💾 Salvar e próximo",type="primary",use_container_width=True,key=f"{prefixo}_salvar")
            with b4:
                finalizar_bt=st.button("🚫 Finalizar",use_container_width=True,key=f"{prefixo}_finalizar")

            if anterior_bt:
                st.session_state["mostrar_ultimo_contato"]=True
                st.rerun()

            if pular_bt:
                with st.spinner("Organizando retorno..."):
                    pular_cliente_por_enquanto(empresa_id)
                st.session_state["flash_contato"]=f"{atual['nome']}: pulado por enquanto. Retorna após 200 contatos."
                st.rerun()

            if finalizar_bt:
                with st.spinner("Salvando..."):
                    finalizar_sem_interesse(empresa_id)
                st.session_state["flash_contato"]=f"{atual['nome']}: finalizado como SEM INTERESSE."
                st.rerun()

            if salvar_bt:
                if st.session_state.get("_salvando_fila"):
                    st.warning("Salvamento em andamento...")
                elif not tipo:
                    st.warning("Selecione o tipo de contato.")
                elif not resultado_ui:
                    st.warning("Selecione o resultado.")
                elif resultado_ui in {"⚠️ Problema no contato/base","📦 Finalização comercial","📄 Cotação / proposta"} and not detalhe:
                    st.warning("Selecione o detalhe.")
                elif resultado_ui=="✅ Falou com cliente" and not proxima:
                    st.warning("Selecione o próximo passo.")
                else:
                    st.session_state["_salvando_fila"]=True
                    try:
                        tipo_map={"📞 Ligação":"LIGAÇÃO","💬 WhatsApp":"WHATSAPP","✉️ E-mail":"E-MAIL","🔹 Outro":"OUTRO"}

                        if resultado_ui=="📵 Não conseguiu contato":
                            resultado="NÃO CONSEGUI CONTATO"; acao="RETORNAR APÓS 200 CONTATOS"; agenda=None
                        elif resultado_ui=="🔥 Cliente interessado":
                            resultado="CLIENTE RESPONDEU"; acao="CLIENTE EM ANDAMENTO"; agenda=None
                        elif resultado_ui=="🚫 Sem interesse":
                            resultado="SEM INTERESSE"; acao=""; agenda=None
                        elif resultado_ui=="📄 Cotação / proposta":
                            mapa={"🧾 Cotação solicitada":"SOLICITOU COTAÇÃO","📤 Cotação enviada":"COTAÇÃO ENVIADA","📄 Proposta enviada":"PROPOSTA ENVIADA"}
                            resultado=mapa[detalhe]; acao="ACOMPANHAR EM CLIENTES EM ANDAMENTO"; agenda=None
                        elif resultado_ui=="📦 Finalização comercial":
                            mapa={"📦 Não utiliza transporte":"NÃO UTILIZA TRANSPORTE","✈️ Já utiliza Azul":"JÁ UTILIZA AZUL"}
                            resultado=mapa[detalhe]; acao=""; agenda=None
                        elif resultado_ui=="⚠️ Problema no contato/base":
                            mapa={"☎️ Contato inválido":"CONTATO INVÁLIDO","👤 Pessoa física / incorreto":"CONTATO PESSOA FÍSICA / INCORRETO",
                                  "📵 Sem telefone":"SEM TELEFONE NA BASE","🔍 Sem contato localizado":"SEM CONTATO LOCALIZADO"}
                            resultado=mapa[detalhe]; acao=""; agenda=None
                        else:
                            mapa={"⏳ Aguardar cliente":"AGUARDANDO CLIENTE","👔 Aguardar responsável":"AGUARDANDO CONTATO DO RESPONSÁVEL",
                                  "📅 Agendar retorno":"RETORNAR EM OUTRA DATA","🔥 Em andamento":"CLIENTE RESPONDEU"}
                            resultado=mapa[proxima]; acao=proxima; agenda=data_ag

                        with st.spinner("Salvando contato..."):
                            status_novo,_,_=registrar_contato(
                                empresa_id,hoje,tipo_map[tipo],resultado,obs,acao,agenda
                            )
                        st.session_state["flash_contato"]=f"✅ Contato salvo — {status_novo}. Próximo cliente carregado."
                        for k in list(st.session_state.keys()):
                            if k.startswith(prefixo):
                                del st.session_state[k]
                        st.rerun()
                    finally:
                        st.session_state["_salvando_fila"]=False

# ---------------- CLIENTES EM ANDAMENTO ----------------
elif menu == "🔥 Clientes em andamento":
    st.subheader("🔥 Clientes em andamento")
    st.caption("Oportunidades que já avançaram além da prospecção inicial.")

    andamento=empresas[empresas["status"].isin(STATUS_EM_ANDAMENTO)].copy()

    if andamento.empty:
        st.info("Nenhum cliente em andamento no momento.")
    else:
        andamento["ag_dt"]=pd.to_datetime(
            andamento["data_agendamento"],
            errors="coerce"
        ).dt.normalize()
        hoje=date.today()
        hoje_ts=pd.Timestamp(hoje).normalize()
        atrasados=int(((andamento["ag_dt"].notna())&(andamento["ag_dt"]<hoje_ts)).sum())
        hoje_qtd=int(((andamento["ag_dt"].notna())&(andamento["ag_dt"]==hoje_ts)).sum())

        a,b,c,d=st.columns(4)
        a.metric("Em andamento",len(andamento))
        b.metric("Retornos atrasados",atrasados)
        c.metric("Retornos hoje",hoje_qtd)
        d.metric("Negociações",int((andamento["status"]=="EM NEGOCIAÇÃO").sum()))

        filtro=st.multiselect(
            "Filtrar status",
            sorted(andamento["status"].dropna().unique().tolist()),
            placeholder="Todos os status"
        )
        view=andamento[andamento["status"].isin(filtro)].copy() if filtro else andamento.copy()

        # Ordena pendências primeiro
        view["_ord"]=view["ag_dt"].apply(lambda d:0 if pd.notna(d) and d<=hoje_ts else 1)
        view=view.sort_values(["_ord","ag_dt","nome"],na_position="last").drop(columns="_ord")

        st.markdown("### Carteira em andamento")
        editor_empresas(view,key_prefix="clientes_andamento")

        st.markdown("### Atualizar andamento")
        mapa={f"{r['nome']} — {r.get('status','')}":int(r["id"]) for _,r in view.head(200).iterrows()}
        escolha=st.selectbox("Cliente",list(mapa.keys()),key="andamento_cliente")
        eid=mapa[escolha]
        emp=view[view["id"]==eid].iloc[0]

        etapa=st.pills(
            "Nova etapa",
            ["⏳ Aguardando cliente","👔 Aguardando responsável","📅 Retorno agendado",
             "🔥 Em andamento","🤝 Reunião agendada","🧾 Cotação solicitada","📤 Cotação enviada",
             "📄 Proposta enviada","💚 Em negociação","🏆 Fechado / ganho","🚫 Sem interesse"],
            selection_mode="single",
            key=f"and_etapa_{eid}"
        )
        obs_a=st.text_area("Observação",value="",key=f"and_obs_{eid}",height=80)
        data_a=None
        if etapa in {"📅 Retorno agendado","🤝 Reunião agendada","⏳ Aguardando cliente","👔 Aguardando responsável"}:
            usar_data=st.checkbox("Definir data de retorno",value=etapa in {"📅 Retorno agendado","🤝 Reunião agendada"},key=f"and_usar_data_{eid}")
            if usar_data:
                data_a=st.date_input("Data",value=hoje+timedelta(days=1),min_value=hoje,format="DD/MM/YYYY",key=f"and_data_{eid}")

        if st.button("💾 Salvar andamento",type="primary",use_container_width=True,key=f"and_salvar_{eid}"):
            if not etapa:
                st.warning("Selecione a etapa.")
            else:
                mapa_result={
                    "⏳ Aguardando cliente":"AGUARDANDO CLIENTE",
                    "👔 Aguardando responsável":"AGUARDANDO CONTATO DO RESPONSÁVEL",
                    "📅 Retorno agendado":"RETORNAR EM OUTRA DATA",
                    "🔥 Em andamento":"CLIENTE RESPONDEU",
                    "🤝 Reunião agendada":"REUNIÃO AGENDADA",
                    "🧾 Cotação solicitada":"SOLICITOU COTAÇÃO",
                    "📤 Cotação enviada":"COTAÇÃO ENVIADA",
                    "📄 Proposta enviada":"PROPOSTA ENVIADA",
                    "💚 Em negociação":"EM NEGOCIAÇÃO",
                    "🏆 Fechado / ganho":"FECHADO",
                    "🚫 Sem interesse":"SEM INTERESSE",
                }
                with st.spinner("Salvando..."):
                    registrar_contato(eid,hoje,"OUTRO",mapa_result[etapa],obs_a,etapa,data_a)
                st.success("Andamento salvo.")
                st.rerun()

# ---------------- IMPORTAÇÃO EM LOTE ----------------
elif menu == "➕ Adicionar contatos em lote":
    st.subheader("➕ Adicionar contatos em lote")

    flash_lote = st.session_state.pop("flash_importacao_lote", None)
    if flash_lote:
        st.success(flash_lote)
    st.caption(
        "Cole os contatos do jeito que você recebeu. O sistema tentará identificar "
        "nome, CPF/CNPJ, e-mail e telefones e mostrará uma prévia antes de incluir."
    )
    st.info(
        "💾 O lote inteiro será salvo de uma vez na base oficial do GitHub. "
        "Só considere concluído quando aparecer a confirmação verde com o total da carteira."
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
                    empresas,
                    r.get("E-mail", "")
                ) else "NÃO",
                axis=1
            )

            previa["Importável"] = previa.apply(
                lambda r: "SIM" if (
                    str(r.get("Nome","")).strip()
                    and tem_identificador_util(
                        r.get("CPF/CNPJ",""),
                        [r.get("Telefone 1",""),r.get("Telefone 2",""),r.get("Telefone 3","")],
                        r.get("E-mail","")
                    )
                ) else "NÃO — falta telefone/e-mail/CPF/CNPJ",
                axis=1
            )
            st.markdown(f"### Prévia — {len(previa)} registro(s) identificado(s)")
            st.dataframe(previa, use_container_width=True, hide_index=True)

            incluir = st.button(
                "Adicionar novos contatos à carteira",
                type="primary",
                use_container_width=True
            )

            if incluir:
                registros_lote = []
                for _, r in previa.iterrows():
                    registros_lote.append({
                        "nome": str(r["Nome"] or "").strip(),
                        "documento": str(r["CPF/CNPJ"] or "").strip(),
                        "email": str(r.get("E-mail","") or "").strip(),
                        "telefones": [
                            r["Telefone 1"],
                            r["Telefone 2"],
                            r["Telefone 3"]
                        ]
                    })

                with st.spinner(
                    f"Salvando {len(registros_lote)} registro(s) na base oficial do GitHub..."
                ):
                    try:
                        incluidos, ignorados, invalidos, sem_identificador = salvar_empresas_em_lote(registros_lote)

                        # Confirma diretamente relendo a base oficial.
                        dados_confirmados = carregar_database(forcar_github=True)
                        total_confirmado = len(dados_confirmados.get("empresas", []))

                        st.session_state["flash_importacao_lote"] = (
                            f"✅ Importação concluída e confirmada no GitHub: "
                            f"{incluidos} incluído(s), {ignorados} duplicado(s), "
                            f"{invalidos} inválido(s), {sem_identificador} ignorado(s) por falta de telefone/e-mail/CPF/CNPJ. "
                            f"Total atual da carteira: "
                            f"{total_confirmado} empresa(s)."
                        )
                        st.rerun()

                    except Exception as e:
                        st.error(
                            "❌ A importação NÃO foi confirmada no GitHub. "
                            "Não feche esta tela e não apague a lista original. "
                            f"Erro: {e}"
                        )

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
    st.caption("Além do nome, informe pelo menos CPF/CNPJ, telefone ou e-mail.")

    with st.form("nova_empresa_final"):
        nome=st.text_input("Nome da empresa / cliente *")
        c1,c2=st.columns(2)
        documento=c1.text_input("CPF ou CNPJ",placeholder="Opcional")
        email=c2.text_input("E-mail",placeholder="Opcional")

        c1,c2,c3=st.columns(3)
        t1=c1.text_input("Telefone 1",placeholder="(00) 00000-0000")
        t2=c2.text_input("Telefone 2",placeholder="(00) 00000-0000")
        t3=c3.text_input("Telefone 3",placeholder="(00) 00000-0000")
        obs=st.text_area("Observação")
        salvar=st.form_submit_button("Salvar cadastro",type="primary")

    if salvar:
        erros=[]
        if not nome.strip():
            erros.append("Informe o nome da empresa/cliente.")
        if not tem_identificador_util(documento,[t1,t2,t3],email):
            erros.append("Informe pelo menos CPF/CNPJ, telefone ou e-mail.")
        if documento and not documento_valido(documento):
            erros.append("O CPF/CNPJ informado é inválido.")
        if email and not email_valido(email):
            erros.append("O e-mail informado é inválido.")
        for rotulo,tel in [("Telefone 1",t1),("Telefone 2",t2),("Telefone 3",t3)]:
            if tel and len(somente_digitos(tel)) not in (10,11):
                erros.append(f"{rotulo} deve ter DDD e 10 ou 11 dígitos.")
        if eh_duplicado(documento,[t1,t2,t3],empresas,email):
            erros.append("Já existe cliente com este CPF/CNPJ, telefone ou e-mail.")

        if erros:
            for e in erros:
                st.error(e)
        else:
            with st.spinner("Salvando..."):
                salvar_empresa(documento,nome,[t1,t2,t3],"SEM CONTATO",obs,"APP",email)
            st.success("Cliente cadastrado e adicionado à fila.")
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

if github_ativo() and st.session_state.get("_github_online", True):
    st.sidebar.success("☁️ GitHub persistente conectado")
elif github_ativo():
    st.sidebar.warning("⚠️ GitHub temporariamente indisponível")
else:
    st.sidebar.error("❌ GitHub NÃO conectado")

if st.sidebar.button("🔄 Carregar base de dados", use_container_width=True):
    try:
        carregar_database(forcar_github=True)
        st.sidebar.success("Base oficial recarregada do GitHub.")
        st.rerun()
    except Exception as e:
        st.sidebar.error(f"Falha ao carregar: {e}")

st.sidebar.caption("Gestão Comercial • PERSISTENTE V9.3 • Fila Compacta")

