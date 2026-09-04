
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
    dados.setdefault("agenda", [])
    dados.setdefault("veiculo_registros", [])
    dados.setdefault("veiculo_tipos", {})
    dados.setdefault("clientes_ticlog", [])
    dados.setdefault("historico_ticlog", [])
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
                    "agenda": [],
                    "veiculo_registros": [],
                    "veiculo_tipos": {},
                    "clientes_ticlog": [],
                    "historico_ticlog": [],
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
    dados.setdefault("agenda", [])
    dados.setdefault("veiculo_registros", [])
    dados.setdefault("veiculo_tipos", {})
    dados.setdefault("clientes_ticlog", [])
    dados.setdefault("historico_ticlog", [])
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
    "AGUARDANDO CLIENTE",
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
            "acoes_base": [],
            "agenda": [],
            "veiculo_registros": [],
            "veiculo_tipos": {},
            "clientes_ticlog": [],
            "historico_ticlog": []
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

    resultados_sem_resposta = {
        "NÃO CONSEGUI CONTATO",
        "MENSAGEM ENVIADA / AGUARDANDO RESPOSTA",
    }
    tentativas_anteriores = sum(
        1 for c in dados["contatos"]
        if int(c.get("empresa_id", 0) or 0) == int(empresa_id)
        and c.get("tipo_contato") != "HISTÓRICO IMPORTADO"
        and str(c.get("resultado") or "").upper() in resultados_sem_resposta
    )
    tentativa_sem_retorno = (
        tentativas_anteriores + 1
        if resultado in resultados_sem_resposta
        else tentativas_anteriores
    )

    seqs = [
        int(c.get("seq_global") or 0)
        for c in dados["contatos"]
        if c.get("seq_global") is not None
    ]
    seq = (max(seqs) if seqs else 0) + 1

    # Status automático conforme o resultado.
    if resultado == "MENSAGEM ENVIADA / AGUARDANDO RESPOSTA":
        if tentativa_sem_retorno >= 3:
            status_novo = "SEM RETORNO APÓS 3 TENTATIVAS"
        else:
            # Continua na fila; ainda não houve avanço comercial.
            status_novo = "AGUARDANDO CLIENTE"

    elif resultado == "NÃO CONSEGUI CONTATO":
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

        # Renomeia por coluna, em vez de substituir a lista inteira.
        # Isso evita ValueError caso novos campos sejam adicionados ao histórico.
        hist_export = hist_export.rename(columns={
            "id": "ID contato",
            "empresa_id": "ID empresa",
            "nome": "Empresa / Cliente",
            "documento": "CPF/CNPJ",
            "email": "E-mail",
            "telefone1": "Telefone 1",
            "telefone2": "Telefone 2",
            "telefone3": "Telefone 3",
            "Data contato": "Data contato",
            "tipo_contato": "Tipo contato",
            "resultado": "Resultado",
            "status_novo": "Status após contato",
            "observacao": "Observação",
            "proxima_acao": "Próxima ação",
            "Data retorno": "Data retorno",
            "seq_global": "Sequência global",
            "criado_em": "Registrado em",
        })
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
                int((empresas["status"]=="FECHADO / GANHO").sum()),
                int((empresas["status"]=="SEM INTERESSE").sum()),
            ]
        })
        resumo.to_excel(writer, index=False, sheet_name="Resumo")

    buffer.seek(0)
    return buffer.getvalue()


# ============================================================
# AGENDA + VEÍCULO DA EMPRESA
# ============================================================

VEICULO_HISTORICO_INICIAL = [{'data': '2025-11-18', 'placa': 'FJL 5J09', 'km_inicial': 166561, 'km_final': 166681, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'Levar veiculo para casa do vendedor', 'observacoes': None, 'abasteceu': 'SIM', 'valor_abastecido': 221.23, 'litros_abastecidos': 52.8, 'tipo_combustivel': 'ETANOL', 'pedagio': 13.4, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '16:09', 'hora_retorno': '19:15', 'linha_origem': 2, 'id_importacao': 'PLANILHA_VEICULO_2026_2', 'tipo_uso': 'USO PESSOAL', 'motivo': 'Levar veiculo para casa do vendedor', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2025-11-22', 'placa': 'FJL 5J09', 'km_inicial': 166681, 'km_final': 166697, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 3, 'id_importacao': 'PLANILHA_VEICULO_2026_3', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2025-11-26', 'placa': 'FJL 5J09', 'km_inicial': 166697, 'km_final': 166711, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 4, 'id_importacao': 'PLANILHA_VEICULO_2026_4', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2025-11-28', 'placa': 'FJL 5J09', 'km_inicial': 166711, 'km_final': 166722, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 5, 'id_importacao': 'PLANILHA_VEICULO_2026_5', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2025-12-03', 'placa': 'FJL 5J09', 'km_inicial': 166722, 'km_final': 166747, 'cliente': None, 'endereco': None, 'cidade_regiao': 'INDAIATUBA', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 6, 'id_importacao': 'PLANILHA_VEICULO_2026_6', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2025-12-05', 'placa': 'FJL 5J09', 'km_inicial': 166747, 'km_final': 166765, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 7, 'id_importacao': 'PLANILHA_VEICULO_2026_7', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2025-12-08', 'placa': 'FJL 5J09', 'km_inicial': 166765, 'km_final': 166781, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 8, 'id_importacao': 'PLANILHA_VEICULO_2026_8', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2025-12-09', 'placa': 'FJL 5J09', 'km_inicial': 166763, 'km_final': 166799, 'cliente': None, 'endereco': None, 'cidade_regiao': 'INDAIATUBA', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 9, 'id_importacao': 'PLANILHA_VEICULO_2026_9', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2025-12-11', 'placa': 'FJL 5J09', 'km_inicial': 166799, 'km_final': 166831, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 10, 'id_importacao': 'PLANILHA_VEICULO_2026_10', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2025-12-15', 'placa': 'FJL 5J09', 'km_inicial': 166831, 'km_final': 166957, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 11, 'id_importacao': 'PLANILHA_VEICULO_2026_11', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2025-12-16', 'placa': 'FJL 5J09', 'km_inicial': 166957, 'km_final': 166871, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 12, 'id_importacao': 'PLANILHA_VEICULO_2026_12', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2025-12-18', 'placa': 'FJL 5J09', 'km_inicial': 166871, 'km_final': 166909, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 13, 'id_importacao': 'PLANILHA_VEICULO_2026_13', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2025-12-19', 'placa': 'FJL 5J09', 'km_inicial': 166909, 'km_final': 166947, 'cliente': None, 'endereco': None, 'cidade_regiao': 'INDAIATUBA', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 14, 'id_importacao': 'PLANILHA_VEICULO_2026_14', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2025-12-20', 'placa': 'FJL 5J09', 'km_inicial': 166947, 'km_final': 167067, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'DEVOLUÇÃO DE VEÍCULO', 'observacoes': None, 'abasteceu': 'NÃO', 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': 16.6, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '11:00', 'hora_retorno': '13:00', 'linha_origem': 15, 'id_importacao': 'PLANILHA_VEICULO_2026_15', 'tipo_uso': 'MANUTENÇÃO VEÍCULO', 'motivo': 'DEVOLUÇÃO DE VEÍCULO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2025-12-20', 'placa': 'FMZ 0A92', 'km_inicial': 242974, 'km_final': 243097, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'Levar Veículo para casa do Vendedor', 'observacoes': None, 'abasteceu': 'NÃO', 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': 24.3, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '16:00', 'hora_retorno': '17:40', 'linha_origem': 16, 'id_importacao': 'PLANILHA_VEICULO_2026_16', 'tipo_uso': 'USO PESSOAL', 'motivo': 'Levar Veículo para casa do Vendedor', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2025-12-22', 'placa': 'FMZ 0A92', 'km_inicial': 243097, 'km_final': 243133, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 17, 'id_importacao': 'PLANILHA_VEICULO_2026_17', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2025-12-23', 'placa': 'FMZ 0A92', 'km_inicial': 243133, 'km_final': 243155, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 18, 'id_importacao': 'PLANILHA_VEICULO_2026_18', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-01-12', 'placa': 'FMZ 0A92', 'km_inicial': 243155, 'km_final': 243183, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 19, 'id_importacao': 'PLANILHA_VEICULO_2026_19', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-01-13', 'placa': 'FMZ 0A92', 'km_inicial': 243183, 'km_final': 243201, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 20, 'id_importacao': 'PLANILHA_VEICULO_2026_20', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-01-14', 'placa': 'FMZ 0A92', 'km_inicial': 243201, 'km_final': 243239, 'cliente': None, 'endereco': None, 'cidade_regiao': 'INDAIATUBA', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 21, 'id_importacao': 'PLANILHA_VEICULO_2026_21', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-01-15', 'placa': 'FMZ 0A92', 'km_inicial': 243239, 'km_final': 243263, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 22, 'id_importacao': 'PLANILHA_VEICULO_2026_22', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-01-16', 'placa': 'FMZ 0A92', 'km_inicial': 243263, 'km_final': 243291, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 23, 'id_importacao': 'PLANILHA_VEICULO_2026_23', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-01-19', 'placa': 'FMZ 0A92', 'km_inicial': 243291, 'km_final': 243327, 'cliente': None, 'endereco': None, 'cidade_regiao': 'INDAIATUBA', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 24, 'id_importacao': 'PLANILHA_VEICULO_2026_24', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-01-20', 'placa': 'FMZ 0A92', 'km_inicial': 243327, 'km_final': 243447, 'cliente': None, 'endereco': 'IPIRANGA', 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'REUNIÃO COMERCIAL AZUL', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '06:30', 'hora_retorno': '17:55', 'linha_origem': 25, 'id_importacao': 'PLANILHA_VEICULO_2026_25', 'tipo_uso': 'OUTROS / CORPORATIVO', 'motivo': 'REUNIÃO COMERCIAL AZUL', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-01-20', 'placa': 'FMZ 0A92', 'km_inicial': 243447, 'km_final': 243592, 'cliente': None, 'endereco': 'IPIRANGA', 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'VOLTA PRA RESIDÊNCIA', 'observacoes': None, 'abasteceu': 'SIM', 'valor_abastecido': 167.65, 'litros_abastecidos': 39.7, 'tipo_combustivel': 'ETANOL', 'pedagio': 24.3, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '17:55', 'hora_retorno': '20:15', 'linha_origem': 26, 'id_importacao': 'PLANILHA_VEICULO_2026_26', 'tipo_uso': 'USO PESSOAL', 'motivo': 'VOLTA PRA RESIDÊNCIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-01-26', 'placa': 'FMZ 0A92', 'km_inicial': 243592, 'km_final': 243608, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 27, 'id_importacao': 'PLANILHA_VEICULO_2026_27', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-01-27', 'placa': 'FMZ 0A92', 'km_inicial': 243608, 'km_final': 243624, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 28, 'id_importacao': 'PLANILHA_VEICULO_2026_28', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-01-28', 'placa': 'FMZ 0A92', 'km_inicial': 243624, 'km_final': 243638, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 29, 'id_importacao': 'PLANILHA_VEICULO_2026_29', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-01-29', 'placa': 'FMZ 0A92', 'km_inicial': 243638, 'km_final': 243660, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '12:30', 'linha_origem': 30, 'id_importacao': 'PLANILHA_VEICULO_2026_30', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-01-30', 'placa': 'FMZ 0A92', 'km_inicial': 243660, 'km_final': 243679, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '12:30', 'linha_origem': 31, 'id_importacao': 'PLANILHA_VEICULO_2026_31', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-01', 'placa': 'FMZ 0A92', 'km_inicial': 243679, 'km_final': 243801, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SAÚDE', 'motivo_original': 'HOSPEDAGEM EM SÃO PAULO', 'observacoes': None, 'abasteceu': 'NÃO', 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': 16.6, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '21:30', 'hora_retorno': '23:00', 'linha_origem': 32, 'id_importacao': 'PLANILHA_VEICULO_2026_32', 'tipo_uso': 'OUTROS / CORPORATIVO', 'motivo': 'HOSPEDAGEM EM SÃO PAULO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-02', 'placa': 'FMZ 0A92', 'km_inicial': 243801, 'km_final': 243812, 'cliente': None, 'endereco': None, 'cidade_regiao': 'JD.SAÚDE', 'motivo_original': 'LOJA SAO12', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '07:50', 'hora_retorno': '08:10', 'linha_origem': 33, 'id_importacao': 'PLANILHA_VEICULO_2026_33', 'tipo_uso': 'LOJA SAO12', 'motivo': 'LOJA SAO12', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-02', 'placa': 'FMZ 0A92', 'km_inicial': 243812, 'km_final': 243820, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 34, 'id_importacao': 'PLANILHA_VEICULO_2026_34', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-03', 'placa': 'FMZ 0A92', 'km_inicial': 243820, 'km_final': 243828, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'HOSPEDAGEM EM SÃO PAULO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '18:00', 'hora_retorno': '18:10', 'linha_origem': 35, 'id_importacao': 'PLANILHA_VEICULO_2026_35', 'tipo_uso': 'OUTROS / CORPORATIVO', 'motivo': 'HOSPEDAGEM EM SÃO PAULO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-03', 'placa': 'FMZ 0A92', 'km_inicial': 243828, 'km_final': 243836, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'LOJA SAO12', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '07:55', 'hora_retorno': '08:12', 'linha_origem': 36, 'id_importacao': 'PLANILHA_VEICULO_2026_36', 'tipo_uso': 'LOJA SAO12', 'motivo': 'LOJA SAO12', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-03', 'placa': 'FMZ 0A92', 'km_inicial': 243836, 'km_final': 243844, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '13:00', 'hora_retorno': '14:00', 'linha_origem': 37, 'id_importacao': 'PLANILHA_VEICULO_2026_37', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-03', 'placa': 'FMZ 0A92', 'km_inicial': 243844, 'km_final': 243859, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'FORTS PEÇAS AUTOMOTIVAS', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '14:00', 'hora_retorno': '14:15', 'linha_origem': 38, 'id_importacao': 'PLANILHA_VEICULO_2026_38', 'tipo_uso': 'MANUTENÇÃO VEÍCULO', 'motivo': 'FORTS PEÇAS AUTOMOTIVAS', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-03', 'placa': 'FMZ 0A92', 'km_inicial': 243859, 'km_final': 243873, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'LOJA SAO12', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '14:15', 'hora_retorno': '14:32', 'linha_origem': 39, 'id_importacao': 'PLANILHA_VEICULO_2026_39', 'tipo_uso': 'LOJA SAO12', 'motivo': 'LOJA SAO12', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-03', 'placa': 'FMZ 0A92', 'km_inicial': 243873, 'km_final': 243881, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'HOSPEDAGEM EM SÃO PAULO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '17:50', 'hora_retorno': '18:00', 'linha_origem': 40, 'id_importacao': 'PLANILHA_VEICULO_2026_40', 'tipo_uso': 'OUTROS / CORPORATIVO', 'motivo': 'HOSPEDAGEM EM SÃO PAULO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-04', 'placa': 'FMZ 0A92', 'km_inicial': 243881, 'km_final': 243889, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'LOJA SAO12', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '07:50', 'hora_retorno': '08:00', 'linha_origem': 41, 'id_importacao': 'PLANILHA_VEICULO_2026_41', 'tipo_uso': 'LOJA SAO12', 'motivo': 'LOJA SAO12', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-04', 'placa': 'FMZ 0A92', 'km_inicial': 243889, 'km_final': 243897, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 42, 'id_importacao': 'PLANILHA_VEICULO_2026_42', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-04', 'placa': 'FMZ 0A92', 'km_inicial': 243903, 'km_final': 243911, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'HOSPEDAGEM EM SÃO PAULO', 'observacoes': None, 'abasteceu': 'SIM', 'valor_abastecido': 224.52, 'litros_abastecidos': 49.14, 'tipo_combustivel': 'ETANOL', 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '18:00', 'hora_retorno': '18:10', 'linha_origem': 43, 'id_importacao': 'PLANILHA_VEICULO_2026_43', 'tipo_uso': 'OUTROS / CORPORATIVO', 'motivo': 'HOSPEDAGEM EM SÃO PAULO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-05', 'placa': 'FMZ 0A92', 'km_inicial': 243911, 'km_final': 243919, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'LOJA SAO12', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '07:50', 'hora_retorno': '08:00', 'linha_origem': 44, 'id_importacao': 'PLANILHA_VEICULO_2026_44', 'tipo_uso': 'LOJA SAO12', 'motivo': 'LOJA SAO12', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-05', 'placa': 'FMZ 0A92', 'km_inicial': 243919, 'km_final': 243927, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': 13.4, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 45, 'id_importacao': 'PLANILHA_VEICULO_2026_45', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-05', 'placa': 'FMZ 0A92', 'km_inicial': 243927, 'km_final': 244047, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'RESIDÊNCIA', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '15:30', 'hora_retorno': '19:00', 'linha_origem': 46, 'id_importacao': 'PLANILHA_VEICULO_2026_46', 'tipo_uso': 'USO PESSOAL', 'motivo': 'RESIDÊNCIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-06', 'placa': 'FMZ 0A92', 'km_inicial': 244047, 'km_final': 244063, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 47, 'id_importacao': 'PLANILHA_VEICULO_2026_47', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-09', 'placa': 'FMZ 0A92', 'km_inicial': 244063, 'km_final': 244084, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 48, 'id_importacao': 'PLANILHA_VEICULO_2026_48', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-10', 'placa': 'FMZ 0A92', 'km_inicial': 244084, 'km_final': 244105, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 49, 'id_importacao': 'PLANILHA_VEICULO_2026_49', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-11', 'placa': 'FMZ 0A92', 'km_inicial': 244105, 'km_final': 244121, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 50, 'id_importacao': 'PLANILHA_VEICULO_2026_50', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-12', 'placa': 'FMZ 0A92', 'km_inicial': 244121, 'km_final': 244137, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 51, 'id_importacao': 'PLANILHA_VEICULO_2026_51', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-13', 'placa': 'FMZ 0A92', 'km_inicial': 244137, 'km_final': 244153, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 52, 'id_importacao': 'PLANILHA_VEICULO_2026_52', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-18', 'placa': 'FMZ 0A92', 'km_inicial': 244153, 'km_final': 244169, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 53, 'id_importacao': 'PLANILHA_VEICULO_2026_53', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-19', 'placa': 'FMZ 0A92', 'km_inicial': 244169, 'km_final': 244202, 'cliente': None, 'endereco': None, 'cidade_regiao': 'CAMPINAS', 'motivo_original': 'TREINAMENTO DE VENDAS', 'observacoes': None, 'abasteceu': 'NÃO', 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': 19.4, 'estacionamento': 40, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '09:00', 'hora_retorno': '17:00', 'linha_origem': 54, 'id_importacao': 'PLANILHA_VEICULO_2026_54', 'tipo_uso': 'OUTROS / CORPORATIVO', 'motivo': 'TREINAMENTO DE VENDAS', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-19', 'placa': 'FMZ 0A92', 'km_inicial': 244202, 'km_final': 244235, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'RETORNO RESIDÊNCIA', 'observacoes': None, 'abasteceu': 'NÃO', 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': 19.4, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '17:00', 'hora_retorno': '18:05', 'linha_origem': 55, 'id_importacao': 'PLANILHA_VEICULO_2026_55', 'tipo_uso': 'USO PESSOAL', 'motivo': 'RETORNO RESIDÊNCIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-20', 'placa': 'FMZ 0A92', 'km_inicial': 244235, 'km_final': 244268, 'cliente': None, 'endereco': None, 'cidade_regiao': 'CAMPINAS', 'motivo_original': 'TREINAMENTO DE VENDAS', 'observacoes': None, 'abasteceu': 'NÃO', 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': 19.4, 'estacionamento': 40, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '08:00', 'hora_retorno': '16:00', 'linha_origem': 56, 'id_importacao': 'PLANILHA_VEICULO_2026_56', 'tipo_uso': 'OUTROS / CORPORATIVO', 'motivo': 'TREINAMENTO DE VENDAS', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-20', 'placa': 'FMZ 0A92', 'km_inicial': 244268, 'km_final': 244301, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'RETORNO RESIDÊNCIA', 'observacoes': None, 'abasteceu': 'NÃO', 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': 19.4, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '16:00', 'hora_retorno': '17:00', 'linha_origem': 57, 'id_importacao': 'PLANILHA_VEICULO_2026_57', 'tipo_uso': 'USO PESSOAL', 'motivo': 'RETORNO RESIDÊNCIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-23', 'placa': 'FMZ 0A92', 'km_inicial': 244301, 'km_final': 244317, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 58, 'id_importacao': 'PLANILHA_VEICULO_2026_58', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-24', 'placa': 'FMZ 0A92', 'km_inicial': 244317, 'km_final': 244333, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 59, 'id_importacao': 'PLANILHA_VEICULO_2026_59', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-25', 'placa': 'FMZ 0A92', 'km_inicial': 244333, 'km_final': 244349, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 60, 'id_importacao': 'PLANILHA_VEICULO_2026_60', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-26', 'placa': 'FMZ 0A92', 'km_inicial': 244349, 'km_final': 244373, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU', 'motivo_original': 'CONSULTA MÉDICA', 'observacoes': None, 'abasteceu': 'SIM', 'valor_abastecido': 100, 'litros_abastecidos': 21.78, 'tipo_combustivel': 'ETANOL', 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '14:00', 'hora_retorno': '15:30', 'linha_origem': 61, 'id_importacao': 'PLANILHA_VEICULO_2026_61', 'tipo_uso': 'USO PESSOAL', 'motivo': 'CONSULTA MÉDICA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-27', 'placa': 'FMZ 0A92', 'km_inicial': 244373, 'km_final': 244395, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 62, 'id_importacao': 'PLANILHA_VEICULO_2026_62', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-03-02', 'placa': 'FMZ 0A92', 'km_inicial': 244395, 'km_final': 244411, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '12:30', 'linha_origem': 63, 'id_importacao': 'PLANILHA_VEICULO_2026_63', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-03', 'placa': 'FMZ 0A92', 'km_inicial': 244411, 'km_final': 244437, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU', 'motivo_original': 'CONSULTA MÉDICA', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '14:15', 'hora_retorno': '15:00', 'linha_origem': 64, 'id_importacao': 'PLANILHA_VEICULO_2026_64', 'tipo_uso': 'USO PESSOAL', 'motivo': 'CONSULTA MÉDICA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-04', 'placa': 'FMZ 0A92', 'km_inicial': 244437, 'km_final': 244458, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'EXAME LABORATORIAL', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '13:30', 'hora_retorno': '14:00', 'linha_origem': 65, 'id_importacao': 'PLANILHA_VEICULO_2026_65', 'tipo_uso': 'USO PESSOAL', 'motivo': 'EXAME LABORATORIAL', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-05', 'placa': 'FMZ 0A92', 'km_inicial': 244458, 'km_final': 244474, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 66, 'id_importacao': 'PLANILHA_VEICULO_2026_66', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-02-06', 'placa': 'FMZ 0A92', 'km_inicial': 244474, 'km_final': 244494, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 67, 'id_importacao': 'PLANILHA_VEICULO_2026_67', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-03-09', 'placa': 'FMZ 0A92', 'km_inicial': 244494, 'km_final': 244510, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 68, 'id_importacao': 'PLANILHA_VEICULO_2026_68', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-03-10', 'placa': 'FMZ 0A92', 'km_inicial': 244510, 'km_final': 244533, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU', 'motivo_original': 'CONSULTA MÉDICA', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '09:15', 'hora_retorno': '10:40', 'linha_origem': 69, 'id_importacao': 'PLANILHA_VEICULO_2026_69', 'tipo_uso': 'USO PESSOAL', 'motivo': 'CONSULTA MÉDICA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-10-11', 'placa': 'FMZ 0A92', 'km_inicial': 244533, 'km_final': 244549, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 70, 'id_importacao': 'PLANILHA_VEICULO_2026_70', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-10-12', 'placa': 'FMZ 0A92', 'km_inicial': 244549, 'km_final': 244565, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 71, 'id_importacao': 'PLANILHA_VEICULO_2026_71', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-10-13', 'placa': 'FMZ 0A92', 'km_inicial': 244565, 'km_final': 244592, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU', 'motivo_original': 'ALMOÇO / RENOVAÇÃO RG', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '14:40', 'linha_origem': 72, 'id_importacao': 'PLANILHA_VEICULO_2026_72', 'tipo_uso': 'USO PESSOAL', 'motivo': 'ALMOÇO / RENOVAÇÃO RG', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-03-16', 'placa': 'FMZ 0A92', 'km_inicial': 244592, 'km_final': 244618, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU', 'motivo_original': 'BUSCAR MEU FILHO NO HOSPITAL', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '17:00', 'hora_retorno': '17:40', 'linha_origem': 73, 'id_importacao': 'PLANILHA_VEICULO_2026_73', 'tipo_uso': 'USO PESSOAL', 'motivo': 'BUSCAR MEU FILHO NO HOSPITAL', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-03-18', 'placa': 'FMZ 0A92', 'km_inicial': 244618, 'km_final': 244670, 'cliente': None, 'endereco': None, 'cidade_regiao': 'CAMPINAS/SUMARÉ', 'motivo_original': 'INAUGURAÇÃO CPQ08', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': 19.4, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:00', 'hora_retorno': '15:45', 'linha_origem': 74, 'id_importacao': 'PLANILHA_VEICULO_2026_74', 'tipo_uso': 'TICLOG CPQ08', 'motivo': 'INAUGURAÇÃO CPQ08', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-03-18', 'placa': 'FMZ 0A92', 'km_inicial': 244670, 'km_final': 244692, 'cliente': None, 'endereco': None, 'cidade_regiao': 'CAMPINAS', 'motivo_original': 'ABASTECER', 'observacoes': None, 'abasteceu': 'SIM', 'valor_abastecido': 156.25, 'litros_abastecidos': 32.6, 'tipo_combustivel': 'ETANOL', 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '15:30', 'hora_retorno': '15:50', 'linha_origem': 75, 'id_importacao': 'PLANILHA_VEICULO_2026_75', 'tipo_uso': 'MANUTENÇÃO VEÍCULO', 'motivo': 'ABASTECER', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2016-03-18', 'placa': 'FMZ 0A92', 'km_inicial': 244692, 'km_final': 244736, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO', 'motivo_original': 'RESIDÊNCIA', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': 19.4, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '15:50', 'hora_retorno': '17:10', 'linha_origem': 76, 'id_importacao': 'PLANILHA_VEICULO_2026_76', 'tipo_uso': 'USO PESSOAL', 'motivo': 'RESIDÊNCIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-03-19', 'placa': 'FMZ 0A92', 'km_inicial': 244736, 'km_final': 244752, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 77, 'id_importacao': 'PLANILHA_VEICULO_2026_77', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2016-03-20', 'placa': 'FMZ 0A92', 'km_inicial': 244752, 'km_final': 244768, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 78, 'id_importacao': 'PLANILHA_VEICULO_2026_78', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-03-23', 'placa': 'FMZ 0A92', 'km_inicial': 244768, 'km_final': 244784, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 79, 'id_importacao': 'PLANILHA_VEICULO_2026_79', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-03-24', 'placa': 'FMZ 0A92', 'km_inicial': 244784, 'km_final': 244800, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 80, 'id_importacao': 'PLANILHA_VEICULO_2026_80', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-03-25', 'placa': 'FMZ 0A92', 'km_inicial': 244800, 'km_final': 244836, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU', 'motivo_original': 'RETORNO MÉDICO FILHO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '09:30', 'hora_retorno': '10:15', 'linha_origem': 81, 'id_importacao': 'PLANILHA_VEICULO_2026_81', 'tipo_uso': 'USO PESSOAL', 'motivo': 'RETORNO MÉDICO FILHO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-06-25', 'placa': 'FMZ 0A92', 'km_inicial': 244836, 'km_final': 244852, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 82, 'id_importacao': 'PLANILHA_VEICULO_2026_82', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-03-26', 'placa': 'FMZ 0A92', 'km_inicial': 244852, 'km_final': 244868, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO', 'motivo_original': 'CLINICA MÉDICA/ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 83, 'id_importacao': 'PLANILHA_VEICULO_2026_83', 'tipo_uso': 'USO PESSOAL', 'motivo': 'CLINICA MÉDICA/ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-03-26', 'placa': 'FMZ 0A92', 'km_inicial': 244868, 'km_final': 244896, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU', 'motivo_original': 'EXAME HOSPITAL UNIMED', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '15:00', 'hora_retorno': '16:45', 'linha_origem': 84, 'id_importacao': 'PLANILHA_VEICULO_2026_84', 'tipo_uso': 'USO PESSOAL', 'motivo': 'EXAME HOSPITAL UNIMED', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-03-27', 'placa': 'FMZ 0A92', 'km_inicial': 244896, 'km_final': 244912, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 85, 'id_importacao': 'PLANILHA_VEICULO_2026_85', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-03-30', 'placa': 'FMZ 0A92', 'km_inicial': 244912, 'km_final': 244948, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU', 'motivo_original': 'ALMOÇO/RETORNO MÉDICO FILHO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '14:00', 'hora_retorno': '15:30', 'linha_origem': 86, 'id_importacao': 'PLANILHA_VEICULO_2026_86', 'tipo_uso': 'USO PESSOAL', 'motivo': 'ALMOÇO/RETORNO MÉDICO FILHO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-03-30', 'placa': 'FMZ 0A92', 'km_inicial': 244948, 'km_final': 245068, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'HOSPEDAGEM EM SÃO PAULO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': 17.4, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '20:45', 'hora_retorno': '22:30', 'linha_origem': 87, 'id_importacao': 'PLANILHA_VEICULO_2026_87', 'tipo_uso': 'OUTROS / CORPORATIVO', 'motivo': 'HOSPEDAGEM EM SÃO PAULO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-03-31', 'placa': 'FMZ 0A92', 'km_inicial': 245068, 'km_final': 245076, 'cliente': 'PANAMEDICAL', 'endereco': 'RUA BORGES LAGOA, 423 V.MARIANA', 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'VISITA SOLICITADA PELO CLIENTE', 'observacoes': 'PROSPECÇÃO', 'abasteceu': 'NÃO', 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '09:30', 'hora_retorno': '10:00', 'linha_origem': 88, 'id_importacao': 'PLANILHA_VEICULO_2026_88', 'tipo_uso': 'VISITA EM CLIENTES', 'motivo': 'VISITA SOLICITADA PELO CLIENTE', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-03-31', 'placa': 'FMZ 0A92', 'km_inicial': 245076, 'km_final': 245084, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:00', 'hora_retorno': '13:00', 'linha_origem': 89, 'id_importacao': 'PLANILHA_VEICULO_2026_89', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-03-31', 'placa': 'FMZ 0A92', 'km_inicial': 245084, 'km_final': 245090, 'cliente': 'DIVERSOS CLIENTES', 'endereco': 'AV. DO CURSINO ', 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'VISITA ', 'observacoes': 'PROSPECÇÃO', 'abasteceu': 'SIM', 'valor_abastecido': 165.12, 'litros_abastecidos': 34.91, 'tipo_combustivel': 'ETANOL', 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '14:00', 'hora_retorno': '14:15', 'linha_origem': 90, 'id_importacao': 'PLANILHA_VEICULO_2026_90', 'tipo_uso': 'VISITA EM CLIENTES', 'motivo': 'VISITA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-03-31', 'placa': 'FMZ 0A92', 'km_inicial': 245090, 'km_final': 245210, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'RETORNO PARA RESIDÊNCIA', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': 14, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '15:00', 'hora_retorno': '18:10', 'linha_origem': 91, 'id_importacao': 'PLANILHA_VEICULO_2026_91', 'tipo_uso': 'USO PESSOAL', 'motivo': 'RETORNO PARA RESIDÊNCIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-04-01', 'placa': 'FMZ 0A92', 'km_inicial': 245210, 'km_final': 245226, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 92, 'id_importacao': 'PLANILHA_VEICULO_2026_92', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-04-02', 'placa': 'FMZ 0A92', 'km_inicial': 245226, 'km_final': 245242, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 93, 'id_importacao': 'PLANILHA_VEICULO_2026_93', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-04-06', 'placa': 'FMZ 0A92', 'km_inicial': 245252, 'km_final': 245268, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 94, 'id_importacao': 'PLANILHA_VEICULO_2026_94', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-04-07', 'placa': 'FMZ 0A92', 'km_inicial': 245268, 'km_final': 245284, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 95, 'id_importacao': 'PLANILHA_VEICULO_2026_95', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-04-08', 'placa': 'FMZ 0A92', 'km_inicial': 245284, 'km_final': 245300, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 96, 'id_importacao': 'PLANILHA_VEICULO_2026_96', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-04-09', 'placa': 'FMZ 0A92', 'km_inicial': 245300, 'km_final': 245316, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 97, 'id_importacao': 'PLANILHA_VEICULO_2026_97', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-04-10', 'placa': 'FMZ 0A92', 'km_inicial': 245316, 'km_final': 245332, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 98, 'id_importacao': 'PLANILHA_VEICULO_2026_98', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-04-13', 'placa': 'FMZ 0A92', 'km_inicial': 245332, 'km_final': 245348, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 99, 'id_importacao': 'PLANILHA_VEICULO_2026_99', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-04-14', 'placa': 'FMZ 0A92', 'km_inicial': 245348, 'km_final': 245399, 'cliente': None, 'endereco': None, 'cidade_regiao': 'CAMPINAS', 'motivo_original': 'EXERCÍCIO ON THE JOB CURSO', 'observacoes': None, 'abasteceu': 'NÃO', 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': 19.4, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '13:00', 'hora_retorno': '14:00', 'linha_origem': 100, 'id_importacao': 'PLANILHA_VEICULO_2026_100', 'tipo_uso': 'TICLOG CPQ08', 'motivo': 'EXERCÍCIO ON THE JOB CURSO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-04-15', 'placa': 'FMZ 0A92', 'km_inicial': 245399, 'km_final': 245450, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'RETORNO PARA RESIDÊNCIA', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': 19.4, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '18:30', 'hora_retorno': '19:30', 'linha_origem': 101, 'id_importacao': 'PLANILHA_VEICULO_2026_101', 'tipo_uso': 'USO PESSOAL', 'motivo': 'RETORNO PARA RESIDÊNCIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-04-16', 'placa': 'FMZ 0A92', 'km_inicial': 245450, 'km_final': 245466, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 102, 'id_importacao': 'PLANILHA_VEICULO_2026_102', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-04-17', 'placa': 'FMZ 0A92', 'km_inicial': 245466, 'km_final': 245484, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 103, 'id_importacao': 'PLANILHA_VEICULO_2026_103', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-04-20', 'placa': 'FMZ 0A92', 'km_inicial': 245484, 'km_final': 245502, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 104, 'id_importacao': 'PLANILHA_VEICULO_2026_104', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-04-22', 'placa': 'FMZ 0A92', 'km_inicial': 245502, 'km_final': 245518, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 105, 'id_importacao': 'PLANILHA_VEICULO_2026_105', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-04-23', 'placa': 'FMZ 0A92', 'km_inicial': 245518, 'km_final': 245534, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 106, 'id_importacao': 'PLANILHA_VEICULO_2026_106', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-04-23', 'placa': 'FMZ 0A92', 'km_inicial': 245534, 'km_final': 245550, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 107, 'id_importacao': 'PLANILHA_VEICULO_2026_107', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-04-27', 'placa': 'FMZ 0A92', 'km_inicial': 245550, 'km_final': 245592, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU/SP', 'motivo_original': 'MÉDICO/ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '14:00', 'hora_retorno': '15:30', 'linha_origem': 108, 'id_importacao': 'PLANILHA_VEICULO_2026_108', 'tipo_uso': 'USO PESSOAL', 'motivo': 'MÉDICO/ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-04-28', 'placa': 'FMZ 0A92', 'km_inicial': 245592, 'km_final': 245608, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 109, 'id_importacao': 'PLANILHA_VEICULO_2026_109', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-04-29', 'placa': 'FMZ 0A92', 'km_inicial': 245608, 'km_final': 245624, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 110, 'id_importacao': 'PLANILHA_VEICULO_2026_110', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-04-30', 'placa': 'FMZ 0A92', 'km_inicial': 245624, 'km_final': 245640, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 111, 'id_importacao': 'PLANILHA_VEICULO_2026_111', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-05-05', 'placa': 'FMZ 0A92', 'km_inicial': 245640, 'km_final': 245656, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 112, 'id_importacao': 'PLANILHA_VEICULO_2026_112', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-05-06', 'placa': 'FMZ 0A92', 'km_inicial': 245656, 'km_final': 245680, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'MÉDICO/ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '14:20', 'linha_origem': 113, 'id_importacao': 'PLANILHA_VEICULO_2026_113', 'tipo_uso': 'USO PESSOAL', 'motivo': 'MÉDICO/ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-05-07', 'placa': 'FMZ 0A92', 'km_inicial': 245680, 'km_final': 245696, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 114, 'id_importacao': 'PLANILHA_VEICULO_2026_114', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-05-08', 'placa': 'FMZ 0A92', 'km_inicial': 245696, 'km_final': 245712, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 115, 'id_importacao': 'PLANILHA_VEICULO_2026_115', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-05-11', 'placa': 'FMZ 0A92', 'km_inicial': 245712, 'km_final': 245728, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 116, 'id_importacao': 'PLANILHA_VEICULO_2026_116', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-05-12', 'placa': 'FMZ 0A92', 'km_inicial': 245728, 'km_final': 245728, 'cliente': 'CARRO PARADO', 'endereco': 'PROBLEMAS NA BATERIA', 'cidade_regiao': 'SALTO/SP', 'motivo_original': None, 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': None, 'hora_retorno': None, 'linha_origem': 117, 'id_importacao': 'PLANILHA_VEICULO_2026_117', 'tipo_uso': 'MANUTENÇÃO VEÍCULO', 'motivo': 'CARRO PARADO - PROBLEMAS NA BATERIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-05-13', 'placa': 'FMZ 0A92', 'km_inicial': 245728, 'km_final': 245728, 'cliente': 'CARRO PARADO', 'endereco': 'PROBLEMAS NA BATERIA', 'cidade_regiao': 'SALTO/SP', 'motivo_original': None, 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': None, 'hora_retorno': None, 'linha_origem': 118, 'id_importacao': 'PLANILHA_VEICULO_2026_118', 'tipo_uso': 'MANUTENÇÃO VEÍCULO', 'motivo': 'CARRO PARADO - PROBLEMAS NA BATERIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-05-14', 'placa': 'FMZ 0A92', 'km_inicial': 245728, 'km_final': 245728, 'cliente': 'CARRO PARADO', 'endereco': 'PROBLEMAS NA BATERIA', 'cidade_regiao': 'SALTO/SP', 'motivo_original': None, 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': None, 'hora_retorno': None, 'linha_origem': 119, 'id_importacao': 'PLANILHA_VEICULO_2026_119', 'tipo_uso': 'MANUTENÇÃO VEÍCULO', 'motivo': 'CARRO PARADO - PROBLEMAS NA BATERIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-05-15', 'placa': 'FMZ 0A92', 'km_inicial': 245728, 'km_final': 245728, 'cliente': 'CARRO PARADO', 'endereco': 'PROBLEMAS NA BATERIA', 'cidade_regiao': 'SALTO/SP', 'motivo_original': None, 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': None, 'hora_retorno': None, 'linha_origem': 120, 'id_importacao': 'PLANILHA_VEICULO_2026_120', 'tipo_uso': 'MANUTENÇÃO VEÍCULO', 'motivo': 'CARRO PARADO - PROBLEMAS NA BATERIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-05-18', 'placa': 'FMZ 0A92', 'km_inicial': 245728, 'km_final': 245728, 'cliente': 'CARRO PARADO', 'endereco': 'PROBLEMAS NA BATERIA', 'cidade_regiao': 'SALTO/SP', 'motivo_original': None, 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': None, 'hora_retorno': None, 'linha_origem': 121, 'id_importacao': 'PLANILHA_VEICULO_2026_121', 'tipo_uso': 'MANUTENÇÃO VEÍCULO', 'motivo': 'CARRO PARADO - PROBLEMAS NA BATERIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-05-19', 'placa': 'FMZ 0A92', 'km_inicial': 245728, 'km_final': 245728, 'cliente': 'CARRO PARADO', 'endereco': 'AGUARDANDO BATERIA', 'cidade_regiao': 'SALTO/SP', 'motivo_original': None, 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': None, 'hora_retorno': None, 'linha_origem': 122, 'id_importacao': 'PLANILHA_VEICULO_2026_122', 'tipo_uso': 'MANUTENÇÃO VEÍCULO', 'motivo': 'CARRO PARADO - AGUARDANDO BATERIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-05-20', 'placa': 'FMZ 0A92', 'km_inicial': 245728, 'km_final': 245728, 'cliente': 'CARRO PARADO', 'endereco': 'AGUARDANDO BATERIA', 'cidade_regiao': 'SALTO/SP', 'motivo_original': None, 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': None, 'hora_retorno': None, 'linha_origem': 123, 'id_importacao': 'PLANILHA_VEICULO_2026_123', 'tipo_uso': 'MANUTENÇÃO VEÍCULO', 'motivo': 'CARRO PARADO - AGUARDANDO BATERIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-05-21', 'placa': 'FMZ 0A92', 'km_inicial': 245728, 'km_final': 245728, 'cliente': 'CARRO PARADO', 'endereco': 'AGUARDANDO BATERIA', 'cidade_regiao': 'SALTO/SP', 'motivo_original': None, 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': None, 'hora_retorno': None, 'linha_origem': 124, 'id_importacao': 'PLANILHA_VEICULO_2026_124', 'tipo_uso': 'MANUTENÇÃO VEÍCULO', 'motivo': 'CARRO PARADO - AGUARDANDO BATERIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-05-22', 'placa': 'FMZ 0A92', 'km_inicial': 245728, 'km_final': 245728, 'cliente': 'CARRO PARADO', 'endereco': 'AGUARDANDO BATERIA', 'cidade_regiao': 'SALTO/SP', 'motivo_original': None, 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': None, 'hora_retorno': None, 'linha_origem': 125, 'id_importacao': 'PLANILHA_VEICULO_2026_125', 'tipo_uso': 'MANUTENÇÃO VEÍCULO', 'motivo': 'CARRO PARADO - AGUARDANDO BATERIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-05-25', 'placa': 'FMZ 0A92', 'km_inicial': 245728, 'km_final': 245728, 'cliente': 'CARRO PARADO', 'endereco': 'AGUARDANDO BATERIA', 'cidade_regiao': 'SALTO/SP', 'motivo_original': None, 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': None, 'hora_retorno': None, 'linha_origem': 126, 'id_importacao': 'PLANILHA_VEICULO_2026_126', 'tipo_uso': 'MANUTENÇÃO VEÍCULO', 'motivo': 'CARRO PARADO - AGUARDANDO BATERIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-05-26', 'placa': 'FMZ 0A92', 'km_inicial': 245728, 'km_final': 245728, 'cliente': 'CARRO PARADO', 'endereco': 'AGUARDANDO BATERIA', 'cidade_regiao': 'SALTO/SP', 'motivo_original': None, 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': None, 'hora_retorno': None, 'linha_origem': 127, 'id_importacao': 'PLANILHA_VEICULO_2026_127', 'tipo_uso': 'MANUTENÇÃO VEÍCULO', 'motivo': 'CARRO PARADO - AGUARDANDO BATERIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-05-27', 'placa': 'FMZ 0A92', 'km_inicial': 245728, 'km_final': 245728, 'cliente': 'CARRO PARADO', 'endereco': 'AGUARDANDO BATERIA', 'cidade_regiao': 'SALTO/SP', 'motivo_original': None, 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': None, 'hora_retorno': None, 'linha_origem': 128, 'id_importacao': 'PLANILHA_VEICULO_2026_128', 'tipo_uso': 'MANUTENÇÃO VEÍCULO', 'motivo': 'CARRO PARADO - AGUARDANDO BATERIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-05-28', 'placa': 'FMZ 0A92', 'km_inicial': 245728, 'km_final': 245728, 'cliente': 'CARRO PARADO', 'endereco': 'AGUARDANDO BATERIA', 'cidade_regiao': 'SALTO/SP', 'motivo_original': None, 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': None, 'hora_retorno': None, 'linha_origem': 129, 'id_importacao': 'PLANILHA_VEICULO_2026_129', 'tipo_uso': 'MANUTENÇÃO VEÍCULO', 'motivo': 'CARRO PARADO - AGUARDANDO BATERIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-05-29', 'placa': 'FMZ 0A92', 'km_inicial': 245728, 'km_final': 245728, 'cliente': 'CARRO PARADO', 'endereco': 'AGUARDANDO BATERIA', 'cidade_regiao': 'SALTO/SP', 'motivo_original': None, 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': None, 'hora_retorno': None, 'linha_origem': 130, 'id_importacao': 'PLANILHA_VEICULO_2026_130', 'tipo_uso': 'MANUTENÇÃO VEÍCULO', 'motivo': 'CARRO PARADO - AGUARDANDO BATERIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-06-01', 'placa': 'FMZ 0A92', 'km_inicial': 245728, 'km_final': 245744, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 131, 'id_importacao': 'PLANILHA_VEICULO_2026_131', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-06-02', 'placa': 'FMZ 0A92', 'km_inicial': 245744, 'km_final': 245760, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 132, 'id_importacao': 'PLANILHA_VEICULO_2026_132', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-06-03', 'placa': 'FMZ 0A92', 'km_inicial': 245760, 'km_final': 245776, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 133, 'id_importacao': 'PLANILHA_VEICULO_2026_133', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-06-08', 'placa': 'FMZ 0A92', 'km_inicial': 245776, 'km_final': 245792, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 134, 'id_importacao': 'PLANILHA_VEICULO_2026_134', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-06-09', 'placa': 'FMZ 0A92', 'km_inicial': 245792, 'km_final': 245808, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 135, 'id_importacao': 'PLANILHA_VEICULO_2026_135', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-06-10', 'placa': 'FMZ 0A92', 'km_inicial': 245808, 'km_final': 245824, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 136, 'id_importacao': 'PLANILHA_VEICULO_2026_136', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-06-11', 'placa': 'FMZ 0A92', 'km_inicial': 245824, 'km_final': 245840, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 137, 'id_importacao': 'PLANILHA_VEICULO_2026_137', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-06-12', 'placa': 'FMZ 0A92', 'km_inicial': 245840, 'km_final': 245867, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU/SP', 'motivo_original': 'ALMOÇO/MEDICO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '14:40', 'linha_origem': 138, 'id_importacao': 'PLANILHA_VEICULO_2026_138', 'tipo_uso': 'USO PESSOAL', 'motivo': 'ALMOÇO/MEDICO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-06-15', 'placa': 'FMZ 0A92', 'km_inicial': 245867, 'km_final': 245883, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 139, 'id_importacao': 'PLANILHA_VEICULO_2026_139', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-06-16', 'placa': 'FMZ 0A92', 'km_inicial': 245883, 'km_final': 245899, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 140, 'id_importacao': 'PLANILHA_VEICULO_2026_140', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-06-17', 'placa': 'FMZ 0A92', 'km_inicial': 245899, 'km_final': 245915, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 141, 'id_importacao': 'PLANILHA_VEICULO_2026_141', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-06-18', 'placa': 'FMZ 0A92', 'km_inicial': 245915, 'km_final': 245931, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 142, 'id_importacao': 'PLANILHA_VEICULO_2026_142', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-06-19', 'placa': 'FMZ 0A92', 'km_inicial': 245931, 'km_final': 245947, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 143, 'id_importacao': 'PLANILHA_VEICULO_2026_143', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-06-22', 'placa': 'FMZ 0A92', 'km_inicial': 245947, 'km_final': 245963, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 144, 'id_importacao': 'PLANILHA_VEICULO_2026_144', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-06-23', 'placa': 'FMZ 0A92', 'km_inicial': 245963, 'km_final': 245979, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 145, 'id_importacao': 'PLANILHA_VEICULO_2026_145', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-06-24', 'placa': 'FMZ 0A92', 'km_inicial': 245979, 'km_final': 245995, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 146, 'id_importacao': 'PLANILHA_VEICULO_2026_146', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-06-25', 'placa': 'FMZ 0A92', 'km_inicial': 245995, 'km_final': 246011, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 147, 'id_importacao': 'PLANILHA_VEICULO_2026_147', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-06-26', 'placa': 'FMZ 0A92', 'km_inicial': 246011, 'km_final': 246027, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 148, 'id_importacao': 'PLANILHA_VEICULO_2026_148', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-06-29', 'placa': 'FMZ 0A92', 'km_inicial': 246027, 'km_final': 243043, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 149, 'id_importacao': 'PLANILHA_VEICULO_2026_149', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-06-30', 'placa': 'FMZ 0A92', 'km_inicial': 246043, 'km_final': 246059, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 150, 'id_importacao': 'PLANILHA_VEICULO_2026_150', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-01', 'placa': 'FMZ 0A92', 'km_inicial': 246059, 'km_final': 246085, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU', 'motivo_original': 'IDA E VOLTA AO PRONTO SOCORRO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '03:20', 'hora_retorno': '04:40', 'linha_origem': 151, 'id_importacao': 'PLANILHA_VEICULO_2026_151', 'tipo_uso': 'USO PESSOAL', 'motivo': 'IDA E VOLTA AO PRONTO SOCORRO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-02', 'placa': 'FMZ 0A92', 'km_inicial': 246085, 'km_final': 246085, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': '***', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '***', 'hora_retorno': '***', 'linha_origem': 152, 'id_importacao': 'PLANILHA_VEICULO_2026_152', 'tipo_uso': 'OUTROS / CORPORATIVO', 'motivo': '***', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-03', 'placa': 'FMZ 0A92', 'km_inicial': 246085, 'km_final': 246085, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': '***', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '***', 'hora_retorno': '***', 'linha_origem': 153, 'id_importacao': 'PLANILHA_VEICULO_2026_153', 'tipo_uso': 'OUTROS / CORPORATIVO', 'motivo': '***', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-06', 'placa': 'FMZ 0A92', 'km_inicial': 246085, 'km_final': 246101, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 154, 'id_importacao': 'PLANILHA_VEICULO_2026_154', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-07', 'placa': 'FMZ 0A92', 'km_inicial': 246101, 'km_final': 246117, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 155, 'id_importacao': 'PLANILHA_VEICULO_2026_155', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-08', 'placa': 'FMZ 0A92', 'km_inicial': 246117, 'km_final': 246133, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 156, 'id_importacao': 'PLANILHA_VEICULO_2026_156', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-09', 'placa': 'FMZ 0A92', 'km_inicial': 246133, 'km_final': 246149, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 157, 'id_importacao': 'PLANILHA_VEICULO_2026_157', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-10', 'placa': 'FMZ0A92', 'km_inicial': 246149, 'km_final': 246165, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 158, 'id_importacao': 'PLANILHA_VEICULO_2026_158', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-13', 'placa': 'FMZ0A92', 'km_inicial': 246165, 'km_final': 246182, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 159, 'id_importacao': 'PLANILHA_VEICULO_2026_159', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-14', 'placa': 'FMZ0A92', 'km_inicial': 246182, 'km_final': 246198, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 160, 'id_importacao': 'PLANILHA_VEICULO_2026_160', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-15', 'placa': 'FMZ0A92', 'km_inicial': 246198, 'km_final': 246125, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 161, 'id_importacao': 'PLANILHA_VEICULO_2026_161', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-16', 'placa': 'FMZ0A92', 'km_inicial': 246125, 'km_final': 246141, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 162, 'id_importacao': 'PLANILHA_VEICULO_2026_162', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-17', 'placa': 'FMZ0A92', 'km_inicial': 246141, 'km_final': 246165, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 163, 'id_importacao': 'PLANILHA_VEICULO_2026_163', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-19', 'placa': 'FMZ0A92', 'km_inicial': 246165, 'km_final': 246287, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'HOSPEDAGEM SÃO PAULO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': 17.4, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '22:00', 'hora_retorno': '23:34', 'linha_origem': 164, 'id_importacao': 'PLANILHA_VEICULO_2026_164', 'tipo_uso': 'OUTROS / CORPORATIVO', 'motivo': 'HOSPEDAGEM SÃO PAULO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-20', 'placa': 'FMZ0A92', 'km_inicial': 246287, 'km_final': 246292, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'SAO12', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '09:00', 'hora_retorno': '13:00', 'linha_origem': 165, 'id_importacao': 'PLANILHA_VEICULO_2026_165', 'tipo_uso': 'LOJA SAO12', 'motivo': 'SAO12', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-20', 'placa': 'FMZ0A92', 'km_inicial': 246292, 'km_final': 246315, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '13:00', 'hora_retorno': '14:00', 'linha_origem': 166, 'id_importacao': 'PLANILHA_VEICULO_2026_166', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-20', 'placa': 'FMZ0A92', 'km_inicial': 246315, 'km_final': 246332, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'SAO12', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '14:07', 'hora_retorno': '16:35', 'linha_origem': 167, 'id_importacao': 'PLANILHA_VEICULO_2026_167', 'tipo_uso': 'LOJA SAO12', 'motivo': 'SAO12', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-20', 'placa': 'FMZ0A92', 'km_inicial': 246332, 'km_final': 246337, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'HOSPEDAGEM SÃO PAULO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '16:35', 'hora_retorno': '16:42', 'linha_origem': 168, 'id_importacao': 'PLANILHA_VEICULO_2026_168', 'tipo_uso': 'OUTROS / CORPORATIVO', 'motivo': 'HOSPEDAGEM SÃO PAULO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-21', 'placa': 'FMZ0A92', 'km_inicial': 246337, 'km_final': 246342, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'SAO12', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '09:00', 'hora_retorno': '09:10', 'linha_origem': 169, 'id_importacao': 'PLANILHA_VEICULO_2026_169', 'tipo_uso': 'LOJA SAO12', 'motivo': 'SAO12', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-21', 'placa': 'FMZ0A92', 'km_inicial': 246342, 'km_final': 246347, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:00', 'hora_retorno': '13:00', 'linha_origem': 170, 'id_importacao': 'PLANILHA_VEICULO_2026_170', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-21', 'placa': 'FMZ0A92', 'km_inicial': 246347, 'km_final': 246355, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'CLIENTE PESCA', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '13:30', 'hora_retorno': '14:45', 'linha_origem': 171, 'id_importacao': 'PLANILHA_VEICULO_2026_171', 'tipo_uso': 'VISITA EM CLIENTES', 'motivo': 'CLIENTE PESCA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-21', 'placa': 'FMZ0A92', 'km_inicial': 246355, 'km_final': 246362, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'AUTAX', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '15:00', 'hora_retorno': '16:20', 'linha_origem': 172, 'id_importacao': 'PLANILHA_VEICULO_2026_172', 'tipo_uso': 'VISITA EM CLIENTES', 'motivo': 'AUTAX', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-21', 'placa': 'FMZ0A92', 'km_inicial': 246362, 'km_final': 246370, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'HOSPEDAGEM SÃO PAULO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '16:20', 'hora_retorno': '16:40', 'linha_origem': 173, 'id_importacao': 'PLANILHA_VEICULO_2026_173', 'tipo_uso': 'OUTROS / CORPORATIVO', 'motivo': 'HOSPEDAGEM SÃO PAULO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-22', 'placa': 'FMZ0A92', 'km_inicial': 246370, 'km_final': 246384, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'ODONTOLOGISTA', 'observacoes': None, 'abasteceu': 'SIM', 'valor_abastecido': 200.53, 'litros_abastecidos': 49.03, 'tipo_combustivel': 'ETANOL', 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '15:20', 'hora_retorno': '17:00', 'linha_origem': 174, 'id_importacao': 'PLANILHA_VEICULO_2026_174', 'tipo_uso': 'USO PESSOAL', 'motivo': 'ODONTOLOGISTA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-22', 'placa': 'FMZ0A92', 'km_inicial': 246384, 'km_final': 246397, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SÃO PAULO', 'motivo_original': 'HOSPEDAGEM SÃO PAULO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '17:00', 'hora_retorno': '17:45', 'linha_origem': 175, 'id_importacao': 'PLANILHA_VEICULO_2026_175', 'tipo_uso': 'OUTROS / CORPORATIVO', 'motivo': 'HOSPEDAGEM SÃO PAULO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-22', 'placa': 'FMZ0A92', 'km_inicial': 246397, 'km_final': 246517, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'VOLTA PRA RESIDÊNCIA', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': 14, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '19:30', 'hora_retorno': '21:10', 'linha_origem': 176, 'id_importacao': 'PLANILHA_VEICULO_2026_176', 'tipo_uso': 'USO PESSOAL', 'motivo': 'VOLTA PRA RESIDÊNCIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-23', 'placa': 'FMZ0A92', 'km_inicial': 246517, 'km_final': 246533, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 177, 'id_importacao': 'PLANILHA_VEICULO_2026_177', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-24', 'placa': 'FMZ0A92', 'km_inicial': 246533, 'km_final': 246549, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 178, 'id_importacao': 'PLANILHA_VEICULO_2026_178', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-27', 'placa': 'FMZ0A92', 'km_inicial': 246549, 'km_final': 246549, 'cliente': None, 'endereco': None, 'cidade_regiao': None, 'motivo_original': 'DISPENSA MÉDICA', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': None, 'hora_retorno': None, 'linha_origem': 179, 'id_importacao': 'PLANILHA_VEICULO_2026_179', 'tipo_uso': 'USO PESSOAL', 'motivo': 'DISPENSA MÉDICA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-28', 'placa': 'FMZ0A92', 'km_inicial': 246549, 'km_final': 246549, 'cliente': None, 'endereco': None, 'cidade_regiao': None, 'motivo_original': 'DISPENSA MÉDICA', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': None, 'hora_retorno': None, 'linha_origem': 180, 'id_importacao': 'PLANILHA_VEICULO_2026_180', 'tipo_uso': 'USO PESSOAL', 'motivo': 'DISPENSA MÉDICA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-29', 'placa': 'FMZ0A92', 'km_inicial': 246549, 'km_final': 246549, 'cliente': None, 'endereco': None, 'cidade_regiao': None, 'motivo_original': 'DISPENSA MÉDICA', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': None, 'hora_retorno': None, 'linha_origem': 181, 'id_importacao': 'PLANILHA_VEICULO_2026_181', 'tipo_uso': 'USO PESSOAL', 'motivo': 'DISPENSA MÉDICA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-30', 'placa': 'FMZ0A92', 'km_inicial': 246549, 'km_final': 246549, 'cliente': None, 'endereco': None, 'cidade_regiao': None, 'motivo_original': 'DISPENSA MÉDICA', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': None, 'hora_retorno': None, 'linha_origem': 182, 'id_importacao': 'PLANILHA_VEICULO_2026_182', 'tipo_uso': 'USO PESSOAL', 'motivo': 'DISPENSA MÉDICA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-07-31', 'placa': 'FMZ0A92', 'km_inicial': 246549, 'km_final': 246549, 'cliente': None, 'endereco': None, 'cidade_regiao': None, 'motivo_original': 'DISPENSA MÉDICA', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': None, 'hora_retorno': None, 'linha_origem': 183, 'id_importacao': 'PLANILHA_VEICULO_2026_183', 'tipo_uso': 'USO PESSOAL', 'motivo': 'DISPENSA MÉDICA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-08-03', 'placa': 'FMZ0A92', 'km_inicial': 246549, 'km_final': 246565, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 184, 'id_importacao': 'PLANILHA_VEICULO_2026_184', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-08-04', 'placa': 'FMZ0A92', 'km_inicial': 246565, 'km_final': 246581, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 185, 'id_importacao': 'PLANILHA_VEICULO_2026_185', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-08-05', 'placa': 'FMZ0A92', 'km_inicial': 246581, 'km_final': 246597, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 186, 'id_importacao': 'PLANILHA_VEICULO_2026_186', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-08-06', 'placa': 'FMZ0A92', 'km_inicial': 246597, 'km_final': 246625, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 187, 'id_importacao': 'PLANILHA_VEICULO_2026_187', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-08-07', 'placa': 'FMZ0A92', 'km_inicial': 246625, 'km_final': 246641, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 188, 'id_importacao': 'PLANILHA_VEICULO_2026_188', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-08-10', 'placa': 'FMZ0A92', 'km_inicial': 246641, 'km_final': 246660, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 189, 'id_importacao': 'PLANILHA_VEICULO_2026_189', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-08-11', 'placa': 'FMZ0A92', 'km_inicial': 246660, 'km_final': 246683, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'BORRACHARIA', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '14:00', 'hora_retorno': '15:20', 'linha_origem': 190, 'id_importacao': 'PLANILHA_VEICULO_2026_190', 'tipo_uso': 'MANUTENÇÃO VEÍCULO', 'motivo': 'BORRACHARIA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-08-12', 'placa': 'FMZ0A92', 'km_inicial': 246683, 'km_final': 246707, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 191, 'id_importacao': 'PLANILHA_VEICULO_2026_191', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-08-13', 'placa': 'FMZ0A92', 'km_inicial': 246707, 'km_final': 246723, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 192, 'id_importacao': 'PLANILHA_VEICULO_2026_192', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-08-14', 'placa': 'FMZ0A92', 'km_inicial': 246723, 'km_final': 246747, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 193, 'id_importacao': 'PLANILHA_VEICULO_2026_193', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-08-17', 'placa': 'FMZ0A92', 'km_inicial': 246747, 'km_final': 246765, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 194, 'id_importacao': 'PLANILHA_VEICULO_2026_194', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-08-18', 'placa': 'FMZ0A92', 'km_inicial': 246765, 'km_final': 246786, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 195, 'id_importacao': 'PLANILHA_VEICULO_2026_195', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-08-19', 'placa': 'FMZ0A92', 'km_inicial': 246786, 'km_final': 246802, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 196, 'id_importacao': 'PLANILHA_VEICULO_2026_196', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-08-20', 'placa': 'FMZ0A92', 'km_inicial': 246802, 'km_final': 246834, 'cliente': None, 'endereco': None, 'cidade_regiao': 'ITU/SP', 'motivo_original': 'CONSULTA MÉDICA', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '08:40', 'hora_retorno': '10:15', 'linha_origem': 197, 'id_importacao': 'PLANILHA_VEICULO_2026_197', 'tipo_uso': 'USO PESSOAL', 'motivo': 'CONSULTA MÉDICA', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-08-21', 'placa': 'FMZ0A92', 'km_inicial': 246834, 'km_final': 246852, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 198, 'id_importacao': 'PLANILHA_VEICULO_2026_198', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-08-24', 'placa': 'FMZ0A92', 'km_inicial': 246852, 'km_final': 246874, 'cliente': None, 'endereco': None, 'cidade_regiao': 'SALTO/SP', 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 199, 'id_importacao': 'PLANILHA_VEICULO_2026_199', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}, {'data': '2026-08-25', 'placa': 'FMZ0A92', 'km_inicial': 246874, 'km_final': None, 'cliente': None, 'endereco': None, 'cidade_regiao': None, 'motivo_original': 'ALMOÇO', 'observacoes': None, 'abasteceu': None, 'valor_abastecido': None, 'litros_abastecidos': None, 'tipo_combustivel': None, 'pedagio': None, 'estacionamento': None, 'outros_gastos': None, 'descricao_outros_gastos': None, 'hora_saida': '12:30', 'hora_retorno': '13:30', 'linha_origem': 200, 'id_importacao': 'PLANILHA_VEICULO_2026_200', 'tipo_uso': 'ALMOÇO', 'motivo': 'ALMOÇO', 'origem': 'PLANILHA HISTÓRICA VEÍCULO', 'criado_em': None, 'usuario': 'IMPORTAÇÃO'}]

VEICULO_TIPOS_PADRAO = {'ALMOÇO': ['ALMOÇO'], 'CDSP2 GDS': ['CDSP2 GDS'], 'LOJA SAO12': ['LOJA SAO12', 'SAO12'], 'MANUTENÇÃO VEÍCULO': ['ABASTECER', 'BORRACHARIA', 'CARRO PARADO - AGUARDANDO BATERIA', 'CARRO PARADO - PROBLEMAS NA BATERIA', 'DEVOLUÇÃO DE VEÍCULO', 'FORTS PEÇAS AUTOMOTIVAS', 'MANUTENÇÃO / REVISÃO'], 'OUTROS / CORPORATIVO': ['***', 'HOSPEDAGEM EM SÃO PAULO', 'HOSPEDAGEM SÃO PAULO', 'REUNIÃO COMERCIAL AZUL', 'TREINAMENTO DE VENDAS'], 'TICLOG CPQ08': ['EXERCÍCIO ON THE JOB CURSO', 'INAUGURAÇÃO CPQ08', 'TICLOG CPQ08'], 'USO PESSOAL': ['ALMOÇO / RENOVAÇÃO RG', 'ALMOÇO/MEDICO', 'ALMOÇO/RETORNO MÉDICO FILHO', 'BUSCAR MEU FILHO NO HOSPITAL', 'CLINICA MÉDICA/ALMOÇO', 'CONSULTA MÉDICA', 'DISPENSA MÉDICA', 'EXAME HOSPITAL UNIMED', 'EXAME LABORATORIAL', 'IDA E VOLTA AO PRONTO SOCORRO', 'Levar Veículo para casa do Vendedor', 'Levar veiculo para casa do vendedor', 'MÉDICO/ALMOÇO', 'ODONTOLOGISTA', 'RESIDÊNCIA', 'RETORNO MÉDICO FILHO', 'RETORNO PARA RESIDÊNCIA', 'RETORNO RESIDÊNCIA', 'VOLTA PRA RESIDÊNCIA'], 'VISITA EM CLIENTES': ['AUTAX', 'CLIENTE PESCA', 'VISITA', 'VISITA SOLICITADA PELO CLIENTE']}

def proximo_id_lista(lista):
    ids = []
    for item in lista:
        try:
            ids.append(int(item.get("id", 0) or 0))
        except Exception:
            pass
    return (max(ids) if ids else 0) + 1

def importar_historico_veiculo_se_necessario():
    """
    Importa uma única vez os 199 registros históricos da planilha enviada.
    A importação só é acionada ao abrir o menu Veículo da empresa.
    Não recria nem substitui nenhuma outra seção do database.json.
    """
    dados = carregar_database(forcar_github=True)
    dados.setdefault("veiculo_registros", [])
    dados.setdefault("veiculo_tipos", {})
    dados.setdefault("metadata", {})

    existentes = {
        str(r.get("id_importacao") or "").strip()
        for r in dados["veiculo_registros"]
        if str(r.get("id_importacao") or "").strip()
    }

    adicionados = 0
    proximo = proximo_id_lista(dados["veiculo_registros"])

    for origem in VEICULO_HISTORICO_INICIAL:
        chave = str(origem.get("id_importacao") or "").strip()
        if chave and chave in existentes:
            continue
        novo = dict(origem)
        novo["id"] = proximo
        proximo += 1
        dados["veiculo_registros"].append(novo)
        if chave:
            existentes.add(chave)
        adicionados += 1

    # Mescla tipos/motivos sem apagar personalizações futuras.
    for tipo, motivos in VEICULO_TIPOS_PADRAO.items():
        atuais = set(dados["veiculo_tipos"].get(tipo, []) or [])
        atuais.update(motivos)
        dados["veiculo_tipos"][tipo] = sorted(atuais)

    if adicionados > 0 or not dados["metadata"].get("veiculo_planilha_importada_v1"):
        dados["metadata"]["veiculo_planilha_importada_v1"] = True
        dados["metadata"]["veiculo_planilha_importada_v1_qtd"] = len(VEICULO_HISTORICO_INICIAL)
        dados["metadata"]["veiculo_planilha_importada_v1_data"] = datetime.now().isoformat(timespec="seconds")
        salvar_database(dados)

    return adicionados

def salvar_compromisso_agenda(data_compromisso, horario, tipo, cliente, local, observacao):
    dados = carregar_database(forcar_github=True)
    dados.setdefault("agenda", [])
    dados["agenda"].append({
        "id": proximo_id_lista(dados["agenda"]),
        "data": data_compromisso.isoformat(),
        "horario": str(horario or "").strip(),
        "tipo": str(tipo or "").strip().upper(),
        "cliente_compromisso": str(cliente or "").strip(),
        "local": str(local or "").strip(),
        "observacao": str(observacao or "").strip(),
        "status": "PROGRAMADO",
        "criado_em": datetime.now().isoformat(timespec="seconds"),
        "usuario": st.session_state.get("usuario_logado", ""),
    })
    salvar_database(dados)

def atualizar_status_agenda(agenda_id, novo_status):
    dados = carregar_database(forcar_github=True)
    dados.setdefault("agenda", [])
    for item in dados["agenda"]:
        if int(item.get("id", 0) or 0) == int(agenda_id):
            item["status"] = novo_status
            item["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
            item["atualizado_por"] = st.session_state.get("usuario_logado", "")
            break
    salvar_database(dados)

def atualizar_compromisso_agenda(
    agenda_id, data_compromisso, horario, tipo, cliente, local, observacao, status
):
    dados = carregar_database(forcar_github=True)
    dados.setdefault("agenda", [])
    encontrado = False

    for item in dados["agenda"]:
        if int(item.get("id", 0) or 0) == int(agenda_id):
            # Preserva origem/TICLOG e demais metadados do compromisso.
            item["data"] = data_compromisso.isoformat() if hasattr(data_compromisso, "isoformat") else str(data_compromisso)
            item["horario"] = horario.strftime("%H:%M") if hasattr(horario, "strftime") else str(horario or "").strip()
            item["tipo"] = str(tipo or "").strip().upper()
            item["cliente_compromisso"] = str(cliente or "").strip()
            item["local"] = str(local or "").strip()
            item["observacao"] = str(observacao or "").strip()
            item["status"] = str(status or "PROGRAMADO").strip().upper()
            item["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
            item["atualizado_por"] = st.session_state.get("usuario_logado", "")
            encontrado = True
            break

    if not encontrado:
        raise RuntimeError("Compromisso não encontrado na agenda.")

    salvar_database(dados)


def excluir_compromisso_agenda(agenda_id):
    dados = carregar_database(forcar_github=True)
    dados.setdefault("agenda", [])

    antes = len(dados["agenda"])
    dados["agenda"] = [
        item for item in dados["agenda"]
        if int(item.get("id", 0) or 0) != int(agenda_id)
    ]

    if len(dados["agenda"]) == antes:
        raise RuntimeError("Compromisso não encontrado na agenda.")

    salvar_database(dados)

def salvar_registro_veiculo(registro):
    dados = carregar_database(forcar_github=True)
    dados.setdefault("veiculo_registros", [])
    novo = dict(registro)
    novo["id"] = proximo_id_lista(dados["veiculo_registros"])
    novo["origem"] = "APP"
    novo["criado_em"] = datetime.now().isoformat(timespec="seconds")
    novo["usuario"] = st.session_state.get("usuario_logado", "")
    novo["id_importacao"] = None
    dados["veiculo_registros"].append(novo)
    salvar_database(dados)

def atualizar_registro_veiculo(registro_id, novos_dados):
    dados = carregar_database(forcar_github=True)
    dados.setdefault("veiculo_registros", [])
    encontrado = False

    for item in dados["veiculo_registros"]:
        if int(item.get("id", 0) or 0) == int(registro_id):
            # Preserva ID, origem, importação e criação; altera somente dados operacionais.
            campos_preservados = {
                "id": item.get("id"),
                "origem": item.get("origem"),
                "id_importacao": item.get("id_importacao"),
                "criado_em": item.get("criado_em"),
            }
            item.update(dict(novos_dados))
            item.update(campos_preservados)
            item["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
            item["atualizado_por"] = st.session_state.get("usuario_logado", "")
            encontrado = True
            break

    if not encontrado:
        raise RuntimeError("Registro do veículo não encontrado.")

    salvar_database(dados)


def excluir_registro_veiculo(registro_id):
    dados = carregar_database(forcar_github=True)
    dados.setdefault("veiculo_registros", [])
    antes = len(dados["veiculo_registros"])
    dados["veiculo_registros"] = [
        r for r in dados["veiculo_registros"]
        if int(r.get("id", 0) or 0) != int(registro_id)
    ]
    if len(dados["veiculo_registros"]) == antes:
        raise RuntimeError("Registro do veículo não encontrado.")
    salvar_database(dados)

def adicionar_tipo_motivo_veiculo(tipo, motivo):
    dados = carregar_database(forcar_github=True)
    dados.setdefault("veiculo_tipos", {})
    tipo = str(tipo or "").strip().upper()
    motivo = str(motivo or "").strip().upper()
    if not tipo:
        return
    atuais = set(dados["veiculo_tipos"].get(tipo, []) or [])
    if motivo:
        atuais.add(motivo)
    dados["veiculo_tipos"][tipo] = sorted(atuais)
    salvar_database(dados)

def normalizar_placa_chave(valor):
    return re.sub(r"[^A-Z0-9]", "", str(valor or "").upper())

def ultimo_km_final(registros, placa):
    chave = normalizar_placa_chave(placa)
    candidatos = []
    for r in registros:
        if normalizar_placa_chave(r.get("placa")) != chave:
            continue
        try:
            km = float(r.get("km_final"))
            if km > 0:
                candidatos.append((str(r.get("data") or ""), int(r.get("id", 0) or 0), km))
        except Exception:
            pass
    if not candidatos:
        return None
    candidatos.sort()
    km = candidatos[-1][2]
    return int(km) if float(km).is_integer() else km

def dataframe_relatorio_veiculo(registros):
    if not registros:
        return pd.DataFrame()

    df = pd.DataFrame(registros).copy()

    colunas_base = [
        "data","placa","km_inicial","km_final","tipo_uso","motivo",
        "cliente","endereco","cidade_regiao","observacoes",
        "abasteceu","valor_abastecido","litros_abastecidos","tipo_combustivel",
        "pedagio","estacionamento","outros_gastos","descricao_outros_gastos",
        "hora_saida","hora_retorno","origem","usuario"
    ]
    for c in colunas_base:
        if c not in df.columns:
            df[c] = None

    ki = pd.to_numeric(df["km_inicial"], errors="coerce")
    kf = pd.to_numeric(df["km_final"], errors="coerce")
    df["km_rodado"] = kf - ki

    gastos = pd.DataFrame({
        "abastecimento": pd.to_numeric(df["valor_abastecido"], errors="coerce").fillna(0),
        "pedagio": pd.to_numeric(df["pedagio"], errors="coerce").fillna(0),
        "estacionamento": pd.to_numeric(df["estacionamento"], errors="coerce").fillna(0),
        "outros": pd.to_numeric(df["outros_gastos"], errors="coerce").fillna(0),
    })
    df["total_gastos"] = gastos.sum(axis=1)

    nomes = {
        "data":"Data",
        "placa":"Placa do veículo",
        "km_inicial":"KM Inicial",
        "km_final":"KM Final",
        "km_rodado":"KM Rodado",
        "tipo_uso":"Tipo de uso",
        "motivo":"Motivo / Situação",
        "cliente":"Cliente",
        "endereco":"Endereço do cliente",
        "cidade_regiao":"Cidade / Região",
        "observacoes":"Observações da visita",
        "abasteceu":"Abasteceu? (Sim/Não)",
        "valor_abastecido":"Valor abastecido (R$)",
        "litros_abastecidos":"Litros abastecidos",
        "tipo_combustivel":"Tipo de combustível",
        "pedagio":"Pedágio (R$)",
        "estacionamento":"Estacionamento (R$)",
        "outros_gastos":"Outros gastos (R$)",
        "descricao_outros_gastos":"Descrição dos outros gastos",
        "total_gastos":"Total de gastos (R$)",
        "hora_saida":"Hora de saída",
        "hora_retorno":"Hora de retorno",
        "origem":"Origem",
        "usuario":"Usuário",
    }

    ordem = [
        "data","placa","km_inicial","km_final","km_rodado","tipo_uso","motivo",
        "cliente","endereco","cidade_regiao","observacoes","abasteceu",
        "valor_abastecido","litros_abastecidos","tipo_combustivel",
        "pedagio","estacionamento","outros_gastos","descricao_outros_gastos",
        "total_gastos","hora_saida","hora_retorno","origem","usuario"
    ]
    return df[ordem].rename(columns=nomes)

def excel_bytes_dataframe(df, nome_aba="Relatório"):
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=nome_aba[:31])
    buffer.seek(0)
    return buffer.getvalue()



# ============================================================
# CLIENTES TICLOG
# ============================================================

CLIENTES_TICLOG_INICIAIS = [{'empresa': 'ABBA LOGISTICA', 'endereco': 'RUA ARMANDO DE CAMPOS, 420', 'telefone': '(19) 99168-9800', 'site': ''}, {'empresa': 'ACCERT TRANSPORTES E LOGÍSTICA LTDA', 'endereco': 'JOÃO GALVÃO ANDERSON, 250', 'telefone': '(19) 99881-4434', 'site': ''}, {'empresa': 'ALPHA CARGO TRANSPORTES LTDA', 'endereco': 'ALFREDO VIEIRA ALVES, 69', 'telefone': '(19) 3212-1430 / (19) 3245-1113', 'site': ''}, {'empresa': 'ATIVA LOGISTICA', 'endereco': 'ALFREDO VIEIRA ALVES, 261', 'telefone': '', 'site': ''}, {'empresa': 'ATIVA DIST. E LOG. LTDA', 'endereco': 'SARA HELENA MANTELLO, 240', 'telefone': '(19) 3282-2121 / (19) 3282-4416', 'site': ''}, {'empresa': 'AUTO POSTO TIC CAMPINAS', 'endereco': 'ANTONIO BOSCATTO, 325', 'telefone': '(19) 3281-2385', 'site': ''}, {'empresa': 'AMALOG', 'endereco': 'LAZARO BIBIANO DA SILVA, 87', 'telefone': '(19) 98222-6533 / (19) 98730-1535', 'site': ''}, {'empresa': 'ARGIUS', 'endereco': 'ANCILA TONINI GAGO, 111', 'telefone': '(19) 99168-9800', 'site': ''}, {'empresa': 'BRASPRESS TRANSPORTES', 'endereco': 'RICARDO DIAS ALVES, 87, 311 E 381', 'telefone': '(19) 2115-0100', 'site': ''}, {'empresa': 'BR7 TRANSPORTES', 'endereco': 'SARA HELENA MANTELLO, 397', 'telefone': '', 'site': ''}, {'empresa': 'BURAN E GOVEIA TRANSPORTES RODOVIÁRIOS DE CARGAS LTDA', 'endereco': 'ARMANDO DE CAMPOS, 367', 'telefone': '(19) 3281-3579', 'site': ''}, {'empresa': 'BYD DO BRASIL', 'endereco': 'ANTONIO BUSCATTO, 230', 'telefone': '(19) 3514-2551 / (19) 3514-2565', 'site': ''}, {'empresa': 'BYD DO BRASIL', 'endereco': 'AV. JOÃO GALVÃO ANDERSON, 250', 'telefone': '(19) 3514-2551 / (19) 3514-2565', 'site': ''}, {'empresa': 'BYD ENERGY', 'endereco': 'JOÃO GALVÃO ANDERSON, 439 E 479', 'telefone': '(19) 3514-2551 / (19) 3514-2565', 'site': ''}, {'empresa': 'CARGO HANDLING TRANSPORTES EXPRESS LTDA.', 'endereco': 'AV. JOÃO GALVÃO ANDERSON, 337', 'telefone': '(19) 3281-0067', 'site': ''}, {'empresa': 'CAROBA TRANSPORTES E COMERCIO LTDA', 'endereco': 'ANCILLA TONINI GAGO, 31', 'telefone': '(19) 3256-0516 / (19) 3113-8001', 'site': ''}, {'empresa': 'CNLOG', 'endereco': 'SARA HELENA MANTELLO, 167', 'telefone': '(19) 98443-5870', 'site': ''}, {'empresa': 'CUNZOLO LOC. E MAQUINAS TRANSPORTES', 'endereco': 'ALFREDO VIEIRA ALVES, 289', 'telefone': '(19) 3281-0922 / (19) 3281-6899', 'site': ''}, {'empresa': 'DISPLAN ENCOMENDAS URGENTES', 'endereco': 'ARMANDO DE CAMPOS, 111', 'telefone': '(19) 3282-0211 / (19) 3282-4673', 'site': ''}, {'empresa': 'DUALTI TRANSPORTES', 'endereco': 'LÁZARO BIBIANO DA SILVA, 67', 'telefone': '(19) 99353-2077', 'site': 'www.dualtitransportes.com.br'}, {'empresa': 'EFFI CARGO LOGISTICS', 'endereco': 'ARMANDO DE CAMPOS, 100', 'telefone': '(19) 3113-4443 / (19) 3113-4440', 'site': ''}, {'empresa': 'EMPÓRIO EXPRESS BEBIDAS E EVENTOS LTDA', 'endereco': 'ALFREDO VIEIRA ALVES, 317', 'telefone': '', 'site': ''}, {'empresa': 'EUCATUR TRANSPORTES E TURISMO LTDA', 'endereco': 'SARA HELENA MANTELLO, 227', 'telefone': '(19) 3116-0300 / (19) 3116-0301', 'site': ''}, {'empresa': 'EUREKA TRANSPORTES LTDA', 'endereco': 'ALFREDO VIEIRA ALVES, 371', 'telefone': '(19) 3281-3075 / (19) 3281-3076', 'site': ''}, {'empresa': 'EBTRANS', 'endereco': 'RUA ADALBERTO PANZAN, 42', 'telefone': '', 'site': ''}, {'empresa': 'EXPRESSO SALOMÉ', 'endereco': 'SARA HELENA MANTELLO, 50', 'telefone': '(19) 3245-2780 / (19) 3245-1105', 'site': ''}, {'empresa': 'GRUPO COLLONI', 'endereco': 'ARMANDO DE CAMPOS, 210', 'telefone': '(11) 98800-5053 / (54) 3359-2400', 'site': ''}, {'empresa': 'GM TENDAS EVENTOS E ARMAZENAGENS LTDA', 'endereco': 'SARA HELENA MANTELLO, 410', 'telefone': '(19) 3291-3756', 'site': ''}, {'empresa': 'HOLD TRANSPORTES', 'endereco': 'LÁZARO BIBIANO DA SILVA, 67', 'telefone': '(19) 99369-8830', 'site': ''}, {'empresa': 'HSE TRANSPORTES', 'endereco': 'RUA ANCILLA TONINI GAGO, 565', 'telefone': '(19) 3281-6555 / (19) 98728-2827', 'site': ''}, {'empresa': 'JAMEF', 'endereco': 'AVENIDA ANTONIO BOSCATTO, 322 A', 'telefone': '2102-2000 / 2102-2019', 'site': ''}, {'empresa': 'JNR TRANSPORTES', 'endereco': 'RUA ARMANDO DE CAMPOS, 120', 'telefone': '(19) 2042-2429 / (35) 99708-5318 / (35) 98413-2790', 'site': 'jnrlogistica.com.br'}, {'empresa': 'M. FERRETI COMERCIO DE IMP. E EXP. LTDA', 'endereco': 'ALFREDO VIEIRA ALVES, 31', 'telefone': '(19) 3241-8844', 'site': ''}, {'empresa': 'MACAMP DISTRIBUIDORA DE PRODUTOS ALIMENTICIOS', 'endereco': 'ARMANDO DE CAMPOS, 500', 'telefone': '(19) 98132-0504', 'site': ''}, {'empresa': 'MARDONIO CARGO EXPRESS TRANSPORTES LTDA', 'endereco': 'ARMANDO DE CAMPOS, 140', 'telefone': '(19) 3267-9799', 'site': ''}, {'empresa': 'MIRA TRANSPORTES LTDA', 'endereco': 'SARA HELENA MANTELLO, 352 / 374', 'telefone': '(19) 2117-9900', 'site': ''}, {'empresa': 'MOSCA LOGISTICA LTDA', 'endereco': 'ANTONIO BUSCATTO, 171', 'telefone': '(19) 3781-2222', 'site': ''}, {'empresa': 'PERCILOG TRANSPORTES', 'endereco': 'LÁZARO BIBIANO DA SILVA, 67', 'telefone': '(19) 98319-0743', 'site': ''}, {'empresa': 'RAK LOG', 'endereco': 'RICARDO DIAS ALVES, 265', 'telefone': '(19) 3800-3725', 'site': ''}, {'empresa': 'RNG TRANSPORTES', 'endereco': 'LÁZARO BIBIANO DA SILVA, 67', 'telefone': '(19) 98198-0623', 'site': 'www.rngtransportes.com.br'}, {'empresa': 'RODOGARCIA TRANSPORTES LTDA', 'endereco': 'ANCILLA TONINI GAGO, 151', 'telefone': '(19) 3781-2230', 'site': ''}, {'empresa': 'RODOMAXLOG ARMAZENAGEM E LOGISTICA LTDA', 'endereco': 'AVENIDA JOÃO GALVÃO ANDERSON, 470', 'telefone': '(19) 3265-5263', 'site': 'www.rodomaxlog.com'}, {'empresa': 'RODOSERGIO / TRANS-OPEN', 'endereco': 'ALFREDO VIEIRA ALVES, 220', 'telefone': '(19) 3281-3277 / (19) 8211-9771', 'site': ''}, {'empresa': 'RUMON TRANSPORTES', 'endereco': 'SARA HELENA MANTELLO, 785', 'telefone': '(19) 2137-6200 / (19) 99760-64480', 'site': ''}, {'empresa': 'RIOTRANS', 'endereco': 'ANCILA TONINI GAGO, 61', 'telefone': '(19) 3513-1834', 'site': ''}, {'empresa': 'RÁPIDO GARIBALDI', 'endereco': 'RUA LAZARO BIBIANO DA SILVA, 361', 'telefone': '', 'site': ''}, {'empresa': 'SÃO RAFAEL TRANSPORTES', 'endereco': 'RUA SARA HELENA MANTELLO, 86', 'telefone': '', 'site': ''}, {'empresa': 'SCHREIBER LOG', 'endereco': 'ANCILA TONINI GAGO, 61', 'telefone': '(19) 2218-7988 / (19) 3281-1963', 'site': ''}, {'empresa': 'SINDICAMP SINDICATO DE EMPRESAS DE TRANSPORTES LTDA', 'endereco': 'ADALBERTO PANZAN, 92', 'telefone': '(19) 3781-6200', 'site': ''}, {'empresa': 'TELEMONT', 'endereco': 'RICARDO DIAS ALVES, 381', 'telefone': '(19) 3284-2600', 'site': ''}, {'empresa': 'TECMAR TRANSPORTES LTDA', 'endereco': 'JOÃO GALVÃO ANDERSON, 1411', 'telefone': '(19) 3281-1611 / (19) 3282-4804', 'site': ''}, {'empresa': 'TELE CARGA EXPRESS TRANSPORTE', 'endereco': 'RUA SARA HELENA MANTELLO, 187', 'telefone': '', 'site': ''}, {'empresa': 'TJ4 TRANSPORTES LTDA (FRILOG)', 'endereco': 'LAZARO BIBIANO DA SILVA, 441', 'telefone': '(19) 3407-4122 / (19) 3407-1521', 'site': ''}, {'empresa': 'TRANS. NASIF', 'endereco': 'ANTONIO BOSCATTO, 50', 'telefone': '(19) 3782-6122', 'site': ''}, {'empresa': 'TRANSPORTADORA RISSO LTDA', 'endereco': 'ADALBERTO PANZAN, 20', 'telefone': '(19) 3781-6700', 'site': ''}, {'empresa': 'TRANSPORTADORA SCARPATTO (TRANSUNI)', 'endereco': 'SARA HELENA MANTELLO, 449', 'telefone': '(19) 3281-0110 / (19) 3281-0060', 'site': ''}, {'empresa': 'TRANSPORTES MAROSO', 'endereco': 'RUA ALFREDO VIEIRA ALVES, 390', 'telefone': '2220-9757', 'site': ''}, {'empresa': 'TRANSPORTES OURO NEGRO LTDA', 'endereco': 'ANCILA TONINI GAGO, 531', 'telefone': '(19) 3014-8065 / (19) 94728-3618', 'site': ''}, {'empresa': 'TRANSUNI TRANSPORTES', 'endereco': 'SARA HELENA MANTELLO, 188', 'telefone': '', 'site': ''}, {'empresa': 'TRLOG TRANSPORTES E LOGÍSTICA EIRELI', 'endereco': 'SARA HELENA MANTELO, 20', 'telefone': '(19) 3282-1124 / (19) 3281-0432', 'site': ''}, {'empresa': 'TRUCK PARK CENTER', 'endereco': 'ARMANDO DE CAMPOS, 455', 'telefone': '(19) 3281-4622', 'site': ''}, {'empresa': 'TRIMAK', 'endereco': 'ARMANDO DE CAMPOS, 180', 'telefone': '(19) 99928-1266', 'site': ''}, {'empresa': 'TRANSMAC', 'endereco': 'RUA SARA HELENA MANTELLO, 448', 'telefone': '(19) 99159-1237', 'site': ''}, {'empresa': 'VALNI TRANSPORTES RODOV. LTDA', 'endereco': 'ANTONIO BOSCATTO, 140', 'telefone': '(19) 3781-5132 / (19) 3781-5157', 'site': ''}, {'empresa': 'XANDÔ', 'endereco': 'LARAZARO BIBIANO, 161', 'telefone': '', 'site': ''}]

TICLOG_STATUS_FINAIS = {
    "SEM INTERESSE",
    "OUTRO SETOR / NÃO É PÚBLICO-ALVO",
    "EMPRESA NÃO EXISTE / ENCERRADA",
}

TICLOG_ACOES = [
    "📞 Ligar",
    "💬 WhatsApp",
    "✉️ E-mail",
    "🚶 Visitar presencialmente",
    "📅 Agendar visita",
]

TICLOG_RESULTADOS = [
    "NÃO ATENDEU / SEM RESPOSTA",
    "FALOU COM RESPONSÁVEL",
    "RETORNAR CONTATO",
    "VISITA PRESENCIAL REALIZADA",
    "VISITA NECESSÁRIA",
    "INTERESSADO",
    "SEM INTERESSE",
    "OUTRO SETOR / NÃO É PÚBLICO-ALVO",
    "EMPRESA NÃO EXISTE / ENCERRADA",
]

def chave_ticlog(empresa, endereco):
    return re.sub(r"\s+", " ", f"{empresa}|{endereco}".strip().upper())

def importar_clientes_ticlog_se_necessario():
    """
    Importação única da carteira TICLOG fornecida.
    Nunca recria o database.json e nunca remove dados existentes.
    """
    dados = carregar_database(forcar_github=True)
    dados.setdefault("clientes_ticlog", [])
    dados.setdefault("historico_ticlog", [])
    dados.setdefault("metadata", {})

    existentes = {
        chave_ticlog(c.get("empresa"), c.get("endereco"))
        for c in dados["clientes_ticlog"]
    }

    prox = proximo_id_lista(dados["clientes_ticlog"])
    adicionados = 0
    agora = datetime.now().isoformat(timespec="seconds")

    for origem in CLIENTES_TICLOG_INICIAIS:
        chave = chave_ticlog(origem.get("empresa"), origem.get("endereco"))
        if chave in existentes:
            continue
        novo = {
            "id": prox,
            "empresa": str(origem.get("empresa") or "").strip(),
            "endereco": str(origem.get("endereco") or "").strip(),
            "telefone": str(origem.get("telefone") or "").strip(),
            "site": str(origem.get("site") or "").strip(),
            "status": "SEM CONTATO",
            "ultima_acao": "",
            "ultima_observacao": "",
            "total_tentativas": 0,
            "data_ultima_acao": None,
            "visita_agendada": 0,
            "data_visita": None,
            "hora_visita": None,
            "criado_em": agora,
            "origem": "LISTA TICLOG INICIAL",
        }
        dados["clientes_ticlog"].append(novo)
        prox += 1
        adicionados += 1
        existentes.add(chave)

    if adicionados > 0 or not dados["metadata"].get("clientes_ticlog_importados_v1"):
        dados["metadata"]["clientes_ticlog_importados_v1"] = True
        dados["metadata"]["clientes_ticlog_importados_v1_qtd"] = len(CLIENTES_TICLOG_INICIAIS)
        dados["metadata"]["clientes_ticlog_importados_v1_data"] = agora
        salvar_database(dados)

    return adicionados

def salvar_cliente_ticlog_novo(empresa, endereco, telefone="", site=""):
    dados = carregar_database(forcar_github=True)
    dados.setdefault("clientes_ticlog", [])
    chave = chave_ticlog(empresa, endereco)
    if any(chave_ticlog(c.get("empresa"), c.get("endereco")) == chave for c in dados["clientes_ticlog"]):
        return False, "Este cliente/endereço já existe na carteira TICLOG."

    dados["clientes_ticlog"].append({
        "id": proximo_id_lista(dados["clientes_ticlog"]),
        "empresa": str(empresa or "").strip().upper(),
        "endereco": str(endereco or "").strip().upper(),
        "telefone": str(telefone or "").strip(),
        "site": str(site or "").strip(),
        "status": "SEM CONTATO",
        "ultima_acao": "",
        "ultima_observacao": "",
        "total_tentativas": 0,
        "data_ultima_acao": None,
        "visita_agendada": 0,
        "data_visita": None,
        "hora_visita": None,
        "criado_em": datetime.now().isoformat(timespec="seconds"),
        "origem": "CADASTRO APP",
    })
    salvar_database(dados)
    return True, "Cliente TICLOG incluído."

def atualizar_cliente_ticlog_cadastro(cliente_id, empresa, endereco, telefone, site):
    dados = carregar_database(forcar_github=True)
    for c in dados.get("clientes_ticlog", []):
        if int(c.get("id", 0) or 0) == int(cliente_id):
            c["empresa"] = str(empresa or "").strip().upper()
            c["endereco"] = str(endereco or "").strip().upper()
            c["telefone"] = str(telefone or "").strip()
            c["site"] = str(site or "").strip()
            c["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
            break
    salvar_database(dados)

def registrar_acao_ticlog(cliente_id, acao, resultado, observacao="", data_visita=None, hora_visita=None):
    """
    Não encerra por ausência de resposta.
    Só sai da carteira ativa em status explicitamente finais.
    Se houver data de visita, cria compromisso na Agenda no mesmo salvamento.
    """
    dados = carregar_database(forcar_github=True)
    dados.setdefault("clientes_ticlog", [])
    dados.setdefault("historico_ticlog", [])
    dados.setdefault("agenda", [])

    cliente = None
    for c in dados["clientes_ticlog"]:
        if int(c.get("id", 0) or 0) == int(cliente_id):
            cliente = c
            break
    if cliente is None:
        raise RuntimeError("Cliente TICLOG não encontrado.")

    agora = datetime.now()
    tentativa = int(cliente.get("total_tentativas", 0) or 0) + 1

    # Sem resposta nunca finaliza: continua ativo para ligação ou visita presencial.
    if resultado == "NÃO ATENDEU / SEM RESPOSTA":
        status = "TENTAR NOVAMENTE / VISITAR"
    elif resultado == "VISITA NECESSÁRIA":
        status = "VISITA A PROGRAMAR"
    elif resultado == "FALOU COM RESPONSÁVEL":
        status = "EM CONTATO"
    elif resultado == "RETORNAR CONTATO":
        status = "RETORNAR CONTATO"
    elif resultado == "VISITA PRESENCIAL REALIZADA":
        status = "VISITA REALIZADA"
    elif resultado == "INTERESSADO":
        status = "INTERESSADO"
    elif resultado in TICLOG_STATUS_FINAIS:
        status = resultado
    else:
        status = "EM ACOMPANHAMENTO"

    data_visita_iso = data_visita.isoformat() if data_visita else None
    hora_visita_txt = hora_visita.strftime("%H:%M") if hasattr(hora_visita, "strftime") else str(hora_visita or "").strip()

    if data_visita:
        status = "VISITA AGENDADA"
        # Evita duplicar a mesma visita TICLOG.
        ja_existe = any(
            str(a.get("origem") or "") == "TICLOG"
            and int(a.get("ticlog_cliente_id", 0) or 0) == int(cliente_id)
            and str(a.get("data") or "") == data_visita_iso
            and str(a.get("horario") or "") == hora_visita_txt
            and str(a.get("status") or "") != "CANCELADO"
            for a in dados["agenda"]
        )
        if not ja_existe:
            dados["agenda"].append({
                "id": proximo_id_lista(dados["agenda"]),
                "data": data_visita_iso,
                "horario": hora_visita_txt,
                "tipo": "VISITA TICLOG",
                "cliente_compromisso": cliente.get("empresa") or "Cliente TICLOG",
                "local": cliente.get("endereco") or "TICLOG",
                "observacao": str(observacao or "").strip(),
                "status": "PROGRAMADO",
                "criado_em": agora.isoformat(timespec="seconds"),
                "usuario": st.session_state.get("usuario_logado", ""),
                "origem": "TICLOG",
                "ticlog_cliente_id": int(cliente_id),
            })

    cliente["status"] = status
    cliente["ultima_acao"] = str(acao or "").strip()
    cliente["ultima_observacao"] = str(observacao or "").strip()
    cliente["total_tentativas"] = tentativa
    cliente["data_ultima_acao"] = agora.date().isoformat()
    cliente["visita_agendada"] = 1 if data_visita else 0
    cliente["data_visita"] = data_visita_iso
    cliente["hora_visita"] = hora_visita_txt if data_visita else None

    dados["historico_ticlog"].append({
        "id": proximo_id_lista(dados["historico_ticlog"]),
        "cliente_id": int(cliente_id),
        "empresa": cliente.get("empresa"),
        "data": agora.date().isoformat(),
        "hora": agora.strftime("%H:%M"),
        "acao": str(acao or "").strip(),
        "resultado": str(resultado or "").strip(),
        "status_novo": status,
        "observacao": str(observacao or "").strip(),
        "data_visita": data_visita_iso,
        "hora_visita": hora_visita_txt if data_visita else None,
        "usuario": st.session_state.get("usuario_logado", ""),
        "criado_em": agora.isoformat(timespec="seconds"),
    })

    salvar_database(dados)
    return status


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

# Navegação em blocos visuais.
if "menu_selected" not in st.session_state:
    st.session_state["menu_selected"] = "📊 Dashboard"

def _nav_button(label, destino, key):
    selecionado = st.session_state.get("menu_selected") == destino
    if st.sidebar.button(
        label,
        key=key,
        use_container_width=True,
        type="primary" if selecionado else "secondary"
    ):
        st.session_state["menu_selected"] = destino
        st.rerun()

st.sidebar.markdown("### COMERCIAL")
_nav_button("📊 Dashboard", "📊 Dashboard", "nav_dashboard")
_nav_button("📞 Fila de contatos", "📞 Fila de contatos", "nav_fila")
_nav_button("🔥 Clientes em andamento", "🔥 Clientes em andamento", "nav_andamento")
_nav_button("📅 Agenda", "📅 Agenda", "nav_agenda")
_nav_button("🏢 Clientes TICLOG", "🏢 Clientes TICLOG", "nav_ticlog")

st.sidebar.divider()
st.sidebar.markdown("### APOIO")
_nav_button("🚗 Veículo da empresa", "🚗 Veículo da empresa", "nav_veiculo")

st.sidebar.divider()
st.sidebar.markdown("### ADMINISTRAÇÃO")
_nav_button("➕ Importar contatos", "➕ Adicionar contatos em lote", "nav_importar")
_nav_button("🏢 Clientes / Editar", "🏢 Consulta / Editar Clientes", "nav_clientes")
_nav_button("➕ Nova empresa", "➕ Nova Empresa", "nav_nova")
_nav_button("📈 Relatórios", "📈 Relatórios", "nav_relatorios")

menu = st.session_state["menu_selected"]

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

    # Integra o histórico TICLOG às métricas comerciais.
    # Cada ação TICLOG conta como contato realizado, mas ausência de resposta
    # continua sendo apenas "sem retorno" e nunca finaliza a carteira TICLOG.
    dados_dash_extra = carregar_database(forcar_github=False)
    hist_ticlog_dash = dados_dash_extra.get("historico_ticlog", []) or []

    if hist_ticlog_dash:
        tic = pd.DataFrame(hist_ticlog_dash).copy()

        def _canal_ticlog(valor):
            v = str(valor or "")
            if "Ligar" in v:
                return "LIGAÇÃO"
            if "WhatsApp" in v:
                return "WHATSAPP"
            if "E-mail" in v:
                return "E-MAIL"
            if "Visitar" in v:
                return "VISITA PRESENCIAL"
            if "Agendar visita" in v:
                return "VISITA / AGENDAMENTO"
            return "OUTRO"

        def _status_ticlog_dashboard(row):
            s = str(row.get("status_novo") or "").strip().upper()
            r = str(row.get("resultado") or "").strip().upper()
            if s == "TENTAR NOVAMENTE / VISITAR" or r == "NÃO ATENDEU / SEM RESPOSTA":
                return "SEM RETORNO"
            if s == "EM CONTATO":
                return "EM ANDAMENTO"
            if s == "RETORNAR CONTATO":
                return "RETORNO AGENDADO"
            if s == "VISITA AGENDADA":
                return "VISITA AGENDADA"
            if s == "VISITA REALIZADA":
                return "EM ANDAMENTO"
            if s == "INTERESSADO":
                return "EM ANDAMENTO"
            return s or r or "SEM CLASSIFICAÇÃO"

        tic_dash = pd.DataFrame({
            "id": tic.get("id"),
            "empresa_id": tic.get("cliente_id").apply(
                lambda x: f"TICLOG-{int(x)}" if pd.notna(x) else None
            ),
            "data_contato": tic.get("data"),
            "tipo_contato": tic.get("acao").apply(_canal_ticlog),
            "resultado": tic.get("resultado"),
            "status_novo": tic.get("status_novo"),
        })
        tic_dash["_data_parse"] = tic_dash["data_contato"].apply(normalizar_data_historico)
        tic_dash["data_dt"] = tic_dash["_data_parse"].apply(
            lambda x: x.date() if pd.notna(x) else None
        )
        tic_dash["status_gerencial"] = tic.apply(_status_ticlog_dashboard, axis=1)
        tic_dash = tic_dash[
            tic_dash["data_dt"].notna() &
            tic_dash["empresa_id"].notna()
        ].copy()

        if analitico.empty:
            analitico = tic_dash.copy()
        else:
            analitico = pd.concat([analitico, tic_dash], ignore_index=True, sort=False)

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
    avanços = int(status_s.isin(["AGUARDANDO CONTATO DO RESPONSÁVEL","RETORNO AGENDADO",
                                 "VISITA AGENDADA","EM ANDAMENTO","REUNIÃO AGENDADA","COTAÇÃO SOLICITADA","COTAÇÃO ENVIADA",
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
        oportunidades=int(status_s.isin(["AGUARDANDO CONTATO DO RESPONSÁVEL","RETORNO AGENDADO",
                                          "VISITA AGENDADA","EM ANDAMENTO","REUNIÃO AGENDADA","COTAÇÃO SOLICITADA","COTAÇÃO ENVIADA",
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
                    "💬 Mensagem enviada / aguardando resposta",
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

                        if resultado_ui=="💬 Mensagem enviada / aguardando resposta":
                            resultado="MENSAGEM ENVIADA / AGUARDANDO RESPOSTA"; acao="AGUARDANDO RESPOSTA"; agenda=None
                        elif resultado_ui=="📵 Não conseguiu contato":
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
    st.markdown("## 🔥 Clientes em andamento")
    st.caption("Oportunidades que já avançaram. Atualize um cliente por vez, como na fila de contatos.")

    flash_and = st.session_state.pop("flash_andamento", None)
    if flash_and:
        st.success(flash_and)

    andamento = empresas[empresas["status"].isin(STATUS_EM_ANDAMENTO)].copy()

    if andamento.empty:
        st.info("Nenhum cliente da carteira geral em andamento no momento.")
    else:
        hoje = date.today()
        hoje_ts = pd.Timestamp(hoje).normalize()

        andamento["ag_dt"] = pd.to_datetime(
            andamento["data_agendamento"],
            errors="coerce"
        ).dt.normalize()

        # Prioridade: retorno atrasado -> retorno hoje -> demais oportunidades.
        def prioridade_andamento(row):
            ag = row.get("ag_dt")
            if pd.notna(ag) and ag < hoje_ts:
                return 1
            if pd.notna(ag) and ag == hoje_ts:
                return 2
            return 3

        andamento["_p"] = andamento.apply(prioridade_andamento, axis=1)
        andamento = andamento.sort_values(
            ["_p", "ag_dt", "nome"],
            na_position="last"
        ).reset_index(drop=True)

        total_and = len(andamento)

        # Navegação local, sem alterar banco ao apenas avançar/voltar.
        if "andamento_pos" not in st.session_state:
            st.session_state["andamento_pos"] = 0

        pos = int(st.session_state.get("andamento_pos", 0) or 0)
        pos = max(0, min(pos, total_and - 1))
        st.session_state["andamento_pos"] = pos

        # Procurar outro cliente, sem tabela grande.
        with st.expander("🔎 Procurar outro cliente em andamento", expanded=False):
            opcoes = {
                f"{r['nome']} — {r.get('status','')}": idx
                for idx, r in andamento.iterrows()
            }
            busca = st.selectbox(
                "Cliente",
                list(opcoes.keys()),
                index=pos if pos < len(opcoes) else 0,
                key="andamento_busca_cliente"
            )
            if st.button("Abrir cliente", use_container_width=True, key="andamento_abrir_cliente"):
                st.session_state["andamento_pos"] = int(opcoes[busca])
                st.rerun()

        atual_and = andamento.iloc[pos]
        eid = int(atual_and["id"])
        prefixo = f"andamento_simple_{eid}"

        # Cabeçalho compacto
        st.markdown(
            f"""
            <div style="padding:.2rem 0 .35rem 0;">
                <div style="display:flex;align-items:center;gap:.55rem;flex-wrap:wrap;">
                    <span style="font-size:1.5rem;font-weight:800;color:#20263a;">
                        {atual_and['nome']}
                    </span>
                    <span style="font-size:.82rem;padding:.18rem .5rem;border-radius:.6rem;
                                 background:#fff0e8;color:#9a3e00;">
                        🔥 EM ANDAMENTO
                    </span>
                </div>
                <div style="margin-top:.35rem;color:#586174;font-size:.9rem;">
                    <b>CPF/CNPJ:</b> {atual_and.get('documento') or '-'}
                    &nbsp;&nbsp;•&nbsp;&nbsp;
                    <b>Status:</b> {atual_and.get('status') or '-'}
                    &nbsp;&nbsp;•&nbsp;&nbsp;
                    <b>Cliente:</b> {pos + 1} de {total_and}
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )

        tels = [
            str(t).strip()
            for t in [
                atual_and.get("telefone1"),
                atual_and.get("telefone2"),
                atual_and.get("telefone3")
            ]
            if str(t or "").strip()
            and str(t or "").strip().upper() not in {"NAN","NONE","NÃO TEM","NAO TEM","-"}
        ]
        contato_txt = " • ".join(tels) if tels else "Sem telefone válido cadastrado"
        email_txt = str(atual_and.get("email") or "").strip()

        linha_contato = f"📞 {contato_txt}"
        if email_txt:
            linha_contato += f" &nbsp;&nbsp;|&nbsp;&nbsp; ✉️ {email_txt}"

        st.markdown(
            f"""
            <div style="background:#f6f8fb;border:1px solid #e6e9ef;border-radius:10px;
                        padding:.65rem .8rem;margin:.25rem 0 .35rem 0;font-size:.95rem;">
                {linha_contato}
            </div>
            """,
            unsafe_allow_html=True
        )

        ultima_obs = str(atual_and.get("observacao_atual") or "").strip()
        prox_acao = str(atual_and.get("proxima_acao") or "").strip()
        data_ag = atual_and.get("data_agendamento")

        if ultima_obs:
            st.caption(f"📝 Última observação: {ultima_obs}")
        if prox_acao:
            texto_prox = f"➡️ Próxima ação: {prox_acao}"
            if pd.notna(data_ag) and str(data_ag).strip() not in {"", "None", "nan"}:
                texto_prox += f" • {data_br(data_ag)}"
            st.caption(texto_prox)

        # Histórico e edição, sem ocupar a tela principal.
        hist_and = contatos[
            contatos["empresa_id"] == eid
        ].copy() if not contatos.empty else pd.DataFrame()

        c_hist, c_edit = st.columns(2)
        with c_hist:
            if not hist_and.empty:
                with st.expander("🕘 Histórico", expanded=False):
                    historico_cliente(contatos, eid)
        with c_edit:
            with st.expander("✏️ Editar cadastro", expanded=False):
                painel_edicao_empresa(atual_and, prefixo="andamento_cadastro")

        st.divider()

        st.markdown("### O que aconteceu agora?")

        etapa = st.pills(
            "Nova etapa",
            [
                "👔 Aguardando responsável",
                "📅 Retorno agendado",
                "🔥 Em andamento",
                "🤝 Reunião agendada",
                "🧾 Cotação solicitada",
                "📤 Cotação enviada",
                "📄 Proposta enviada",
                "💚 Em negociação",
                "🏆 Fechado / ganho",
                "🚫 Sem interesse",
            ],
            selection_mode="single",
            key=f"{prefixo}_etapa"
        )

        data_nova = None
        if etapa in {
            "📅 Retorno agendado",
            "🤝 Reunião agendada",
            "👔 Aguardando responsável"
        }:
            usar_data = st.checkbox(
                "📅 Definir data de retorno",
                value=etapa in {"📅 Retorno agendado", "🤝 Reunião agendada"},
                key=f"{prefixo}_usar_data"
            )
            if usar_data:
                data_nova = st.date_input(
                    "Data do retorno",
                    value=hoje + timedelta(days=1),
                    min_value=hoje,
                    format="DD/MM/YYYY",
                    key=f"{prefixo}_data"
                )

        obs_a = st.text_area(
            "📝 Observação (opcional)",
            placeholder="Ex.: cliente pediu proposta revisada; retornar amanhã.",
            key=f"{prefixo}_obs",
            height=85
        )

        st.divider()

        b1, b2, b3 = st.columns([1, 1, 1.8])

        with b1:
            anterior_bt = st.button(
                "⬅️ Anterior",
                use_container_width=True,
                key=f"{prefixo}_anterior"
            )
        with b2:
            pular_bt = st.button(
                "⏭️ Pular",
                use_container_width=True,
                key=f"{prefixo}_pular"
            )
        with b3:
            salvar_bt = st.button(
                "💾 Salvar e próximo",
                type="primary",
                use_container_width=True,
                key=f"{prefixo}_salvar"
            )

        if anterior_bt:
            st.session_state["andamento_pos"] = (pos - 1) % total_and
            st.rerun()

        if pular_bt:
            # Navega sem registrar contato, sem alterar status e sem gravar no banco.
            st.session_state["andamento_pos"] = (pos + 1) % total_and
            st.rerun()

        if salvar_bt:
            if not etapa:
                st.warning("Selecione a nova etapa.")
            else:
                mapa_result = {
                    "👔 Aguardando responsável": "AGUARDANDO CONTATO DO RESPONSÁVEL",
                    "📅 Retorno agendado": "RETORNAR EM OUTRA DATA",
                    "🔥 Em andamento": "CLIENTE RESPONDEU",
                    "🤝 Reunião agendada": "REUNIÃO AGENDADA",
                    "🧾 Cotação solicitada": "SOLICITOU COTAÇÃO",
                    "📤 Cotação enviada": "COTAÇÃO ENVIADA",
                    "📄 Proposta enviada": "PROPOSTA ENVIADA",
                    "💚 Em negociação": "EM NEGOCIAÇÃO",
                    "🏆 Fechado / ganho": "FECHADO",
                    "🚫 Sem interesse": "SEM INTERESSE",
                }

                finaliza = etapa in {"🏆 Fechado / ganho", "🚫 Sem interesse"}

                with st.spinner("Salvando andamento..."):
                    registrar_contato(
                        eid,
                        hoje,
                        "OUTRO",
                        mapa_result[etapa],
                        obs_a,
                        etapa,
                        data_nova
                    )

                # Se saiu do menu, o próximo ocupou a mesma posição.
                # Se permaneceu, avança uma posição.
                if finaliza:
                    st.session_state["andamento_pos"] = min(pos, max(total_and - 2, 0))
                else:
                    st.session_state["andamento_pos"] = (pos + 1) % total_and

                st.session_state["flash_andamento"] = f"✅ {atual_and['nome']}: andamento salvo."
                st.rerun()


    # TICLOG também aparece em Clientes em andamento quando houve avanço real.
    dados_and_tic = carregar_database(forcar_github=False)
    clientes_tic_and = dados_and_tic.get("clientes_ticlog", []) or []
    status_tic_avancados = {
        "EM CONTATO",
        "RETORNAR CONTATO",
        "VISITA AGENDADA",
        "VISITA REALIZADA",
        "INTERESSADO",
    }
    tic_and = [
        c for c in clientes_tic_and
        if str(c.get("status") or "").upper() in status_tic_avancados
    ]

    st.divider()
    st.markdown("### 🏢 TICLOG em andamento")
    st.caption(
        "Clientes TICLOG que já tiveram avanço real. "
        "Sem contato, tentativa sem resposta e visita ainda sem data continuam somente na carteira TICLOG."
    )

    if not tic_and:
        st.caption("Nenhum cliente TICLOG em andamento no momento.")
    else:
        st.metric("Clientes TICLOG em andamento", len(tic_and))
        tic_and = sorted(
            tic_and,
            key=lambda c: (
                str(c.get("data_visita") or "9999-12-31"),
                str(c.get("empresa") or "")
            )
        )
        for c in tic_and:
            data_vis = pd.to_datetime(c.get("data_visita"), errors="coerce")
            data_vis_txt = data_vis.strftime("%d/%m/%Y") if pd.notna(data_vis) else ""
            complemento = ""
            if data_vis_txt:
                complemento = f" • 📅 {data_vis_txt} {c.get('hora_visita') or ''}".strip()
            st.markdown(
                f"""
                <div style="background:#f7f9fc;border:1px solid #e4e8ef;border-radius:12px;
                            padding:.7rem .9rem;margin:.3rem 0;">
                    <div style="font-weight:800;">{c.get('empresa') or '-'}</div>
                    <div style="font-size:.85rem;color:#5f6878;">
                        {c.get('status') or '-'}{complemento}
                    </div>
                    <div style="font-size:.82rem;color:#667085;margin-top:.15rem;">
                        📞 {c.get('telefone') or 'Sem telefone'} • 📍 {c.get('endereco') or '-'}
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

        if st.button(
            "🏢 Abrir Clientes TICLOG",
            key="andamento_abrir_ticlog",
            use_container_width=True
        ):
            st.session_state["menu_selected"] = "🏢 Clientes TICLOG"
            st.rerun()

    # Inclusão direta de oportunidade que não veio da fila de prospecção.
    st.divider()
    with st.expander("➕ Incluir cliente direto em andamento", expanded=False):
        st.caption(
            "Use para clientes que já estão em negociação/acompanhamento e não precisam passar pela Fila de contatos."
        )

        with st.form("form_cliente_direto_andamento", clear_on_submit=True):
            nome_direto = st.text_input(
                "Nome / Empresa *",
                placeholder="Nome da empresa ou cliente"
            )

            c1, c2 = st.columns(2)
            documento_direto = c1.text_input(
                "CPF/CNPJ",
                placeholder="Opcional"
            )
            telefone_direto = c2.text_input(
                "Telefone",
                placeholder="(00) 00000-0000"
            )

            c3, c4 = st.columns([1.2, 1])
            email_direto = c3.text_input(
                "E-mail",
                placeholder="Opcional"
            )
            status_direto_ui = c4.selectbox(
                "Status inicial *",
                [
                    "👔 Aguardando responsável",
                    "📅 Retorno agendado",
                    "🔥 Em andamento",
                    "🤝 Reunião agendada",
                    "🧾 Cotação solicitada",
                    "📤 Cotação enviada",
                    "📄 Proposta enviada",
                    "💚 Em negociação",
                ]
            )

            salvar_direto = st.form_submit_button(
                "➕ Incluir em Clientes em andamento",
                type="primary",
                use_container_width=True
            )

        if salvar_direto:
            erros = []

            if not str(nome_direto or "").strip():
                erros.append("Informe o nome da empresa/cliente.")

            if not tem_identificador_util(
                documento_direto,
                [telefone_direto],
                email_direto
            ):
                erros.append("Informe pelo menos CPF/CNPJ, telefone ou e-mail.")

            if documento_direto and not documento_valido(documento_direto):
                erros.append("O CPF/CNPJ informado é inválido.")

            if telefone_direto and len(somente_digitos(telefone_direto)) not in (10, 11):
                erros.append("O telefone deve ter DDD e 10 ou 11 dígitos.")

            if email_direto and not email_valido(email_direto):
                erros.append("O e-mail informado é inválido.")

            if eh_duplicado(
                documento_direto,
                [telefone_direto],
                empresas,
                email_direto
            ):
                erros.append("Já existe cliente com este CPF/CNPJ, telefone ou e-mail.")

            mapa_status_direto = {
                "👔 Aguardando responsável": "AGUARDANDO CONTATO DO RESPONSÁVEL",
                "📅 Retorno agendado": "RETORNO AGENDADO",
                "🔥 Em andamento": "EM ANDAMENTO",
                "🤝 Reunião agendada": "REUNIÃO AGENDADA",
                "🧾 Cotação solicitada": "COTAÇÃO SOLICITADA",
                "📤 Cotação enviada": "COTAÇÃO ENVIADA",
                "📄 Proposta enviada": "PROPOSTA ENVIADA",
                "💚 Em negociação": "EM NEGOCIAÇÃO",
            }

            if erros:
                for erro in erros:
                    st.error(erro)
            else:
                with st.spinner("Incluindo cliente em andamento..."):
                    salvar_empresa(
                        documento_direto,
                        nome_direto,
                        [telefone_direto, "", ""],
                        mapa_status_direto[status_direto_ui],
                        "",
                        "INCLUSÃO DIRETA EM ANDAMENTO",
                        email_direto
                    )

                st.session_state["flash_andamento"] = (
                    f"✅ {str(nome_direto).strip().upper()} incluído diretamente em Clientes em andamento."
                )
                st.rerun()


# ---------------- AGENDA ----------------

elif menu == "🏢 Clientes TICLOG":
    st.markdown("## 🏢 Clientes TICLOG")
    st.caption(
        "Carteira de prospecção local. Sem resposta não encerra o cliente: "
        "continuamos tentando contato e, quando necessário, visitamos presencialmente."
    )

    adicionados = importar_clientes_ticlog_se_necessario()
    if adicionados > 0:
        st.success(f"✅ Carteira TICLOG carregada: {adicionados} novo(s) cliente(s).")

    dados_t = carregar_database(forcar_github=False)
    clientes_t = dados_t.get("clientes_ticlog", []) or []
    historico_t = dados_t.get("historico_ticlog", []) or []

    if not clientes_t:
        st.info("Nenhum cliente TICLOG cadastrado.")
    else:
        df_t = pd.DataFrame(clientes_t)
        for col in ["status","empresa","endereco","telefone","site","data_visita","hora_visita","total_tentativas"]:
            if col not in df_t.columns:
                df_t[col] = None

        finais_t = df_t["status"].isin(TICLOG_STATUS_FINAIS)
        ativos_t = df_t[~finais_t].copy()

        total = len(df_t)
        ativos_qtd = len(ativos_t)
        visitas_qtd = int((ativos_t["status"] == "VISITA AGENDADA").sum()) if not ativos_t.empty else 0
        sem_contato_qtd = int((ativos_t["status"] == "SEM CONTATO").sum()) if not ativos_t.empty else 0

        q1,q2,q3,q4 = st.columns(4)
        q1.metric("Carteira TICLOG", total)
        q2.metric("Ativos", ativos_qtd)
        q3.metric("Sem contato", sem_contato_qtd)
        q4.metric("Visitas agendadas", visitas_qtd)

        # Prioridade: visita mais próxima -> sem contato -> demais ativos.
        if not ativos_t.empty:
            ativos_t["visita_dt"] = pd.to_datetime(ativos_t["data_visita"], errors="coerce")
            hoje_t = pd.Timestamp(date.today())
            ativos_t["_p"] = 3
            ativos_t.loc[ativos_t["status"] == "SEM CONTATO", "_p"] = 1
            ativos_t.loc[ativos_t["status"] == "VISITA AGENDADA", "_p"] = 0
            ativos_t = ativos_t.sort_values(
                ["_p","visita_dt","total_tentativas","empresa"],
                na_position="last"
            ).reset_index(drop=True)

        if ativos_t.empty:
            st.success("Todos os clientes TICLOG estão em status final.")
        else:
            if "ticlog_pos" not in st.session_state:
                st.session_state["ticlog_pos"] = 0
            pos = int(st.session_state.get("ticlog_pos", 0) or 0)
            pos = max(0, min(pos, len(ativos_t)-1))
            st.session_state["ticlog_pos"] = pos

            with st.expander("🔎 Procurar cliente TICLOG", expanded=False):
                mapa_busca = {
                    f"{r['empresa']} — {r.get('status','')}": idx
                    for idx, r in ativos_t.iterrows()
                }
                esc = st.selectbox("Cliente", list(mapa_busca.keys()), index=pos, key="ticlog_busca")
                if st.button("Abrir cliente", key="ticlog_abrir", use_container_width=True):
                    st.session_state["ticlog_pos"] = int(mapa_busca[esc])
                    st.rerun()

            atual = ativos_t.iloc[pos]
            tid = int(atual["id"])

            tag = "📍 TICLOG"
            if atual.get("status") == "VISITA AGENDADA":
                tag = "📅 VISITA AGENDADA"
            elif atual.get("status") == "SEM CONTATO":
                tag = "🆕 SEM CONTATO"

            st.markdown(
                f"""
                <div style="padding:.2rem 0 .4rem 0;">
                    <div style="display:flex;align-items:center;gap:.55rem;flex-wrap:wrap;">
                        <span style="font-size:1.5rem;font-weight:800;color:#20263a;">
                            {atual.get('empresa') or '-'}
                        </span>
                        <span style="font-size:.8rem;padding:.18rem .5rem;border-radius:.6rem;
                                     background:#eef3ff;">{tag}</span>
                    </div>
                    <div style="margin-top:.35rem;color:#586174;font-size:.9rem;">
                        <b>Status:</b> {atual.get('status') or 'SEM CONTATO'}
                        &nbsp;•&nbsp;
                        <b>Tentativas:</b> {int(atual.get('total_tentativas') or 0)}
                        &nbsp;•&nbsp;
                        <b>Cliente:</b> {pos+1} de {len(ativos_t)}
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

            st.markdown(
                f"""
                <div style="background:#f6f8fb;border:1px solid #e6e9ef;border-radius:10px;
                            padding:.7rem .9rem;margin:.2rem 0 .4rem 0;">
                    📍 <b>{atual.get('endereco') or 'Endereço não informado'}</b><br>
                    📞 {atual.get('telefone') or 'Sem telefone'}<br>
                    🌐 {atual.get('site') or 'Sem site informado'}
                </div>
                """,
                unsafe_allow_html=True
            )

            if str(atual.get("ultima_observacao") or "").strip():
                st.caption(f"📝 Última observação: {atual.get('ultima_observacao')}")
            if atual.get("status") == "VISITA AGENDADA" and atual.get("data_visita"):
                dtv = pd.to_datetime(atual.get("data_visita"), errors="coerce")
                dtv_txt = dtv.strftime("%d/%m/%Y") if pd.notna(dtv) else atual.get("data_visita")
                st.info(f"📅 Visita: **{dtv_txt} às {atual.get('hora_visita') or '--:--'}**")

            c_hist, c_edit = st.columns(2)
            with c_hist:
                hist_cliente = [
                    h for h in historico_t
                    if int(h.get("cliente_id",0) or 0) == tid
                ]
                with st.expander("🕘 Histórico", expanded=False):
                    if not hist_cliente:
                        st.caption("Sem histórico ainda.")
                    else:
                        hdf = pd.DataFrame(hist_cliente)
                        if "data" in hdf.columns:
                            hdf["data"] = pd.to_datetime(hdf["data"], errors="coerce").dt.strftime("%d/%m/%Y")
                        cols_h = [c for c in ["data","hora","acao","resultado","status_novo","observacao","data_visita","hora_visita"] if c in hdf.columns]
                        st.dataframe(hdf[cols_h].iloc[::-1], use_container_width=True, hide_index=True)

            with c_edit:
                with st.expander("✏️ Editar cadastro", expanded=False):
                    e_nome = st.text_input("Empresa", value=str(atual.get("empresa") or ""), key=f"tic_nome_{tid}")
                    e_end = st.text_input("Endereço", value=str(atual.get("endereco") or ""), key=f"tic_end_{tid}")
                    e_tel = st.text_input("Telefone", value=str(atual.get("telefone") or ""), key=f"tic_tel_{tid}")
                    e_site = st.text_input("Site", value=str(atual.get("site") or ""), key=f"tic_site_{tid}")
                    if st.button("Salvar cadastro", key=f"tic_edit_salvar_{tid}", use_container_width=True):
                        atualizar_cliente_ticlog_cadastro(tid, e_nome, e_end, e_tel, e_site)
                        st.success("Cadastro atualizado.")
                        st.rerun()

            st.divider()
            st.markdown("### Próxima ação")

            acao_t = st.pills(
                "Ação",
                TICLOG_ACOES,
                selection_mode="single",
                key=f"tic_acao_{tid}"
            )

            resultado_t = st.pills(
                "Resultado",
                TICLOG_RESULTADOS,
                selection_mode="single",
                key=f"tic_resultado_{tid}"
            )

            obs_t = st.text_area(
                "Observação (opcional)",
                placeholder="Ex.: ninguém atendeu; fazer visita presencial na próxima ida ao TICLOG.",
                height=75,
                key=f"tic_obs_{tid}"
            )

            data_visita_t = None
            hora_visita_t = None
            if acao_t == "📅 Agendar visita" or resultado_t == "VISITA NECESSÁRIA":
                agendar_agora = st.checkbox(
                    "Já tenho a data da visita",
                    value=acao_t == "📅 Agendar visita",
                    key=f"tic_tem_data_{tid}"
                )
                if agendar_agora:
                    a,b = st.columns(2)
                    data_visita_t = a.date_input(
                        "Data da visita *",
                        value=date.today()+timedelta(days=1),
                        min_value=date.today(),
                        format="DD/MM/YYYY",
                        key=f"tic_data_visita_{tid}"
                    )
                    hora_visita_t = b.time_input(
                        "Horário *",
                        value=datetime.now().replace(second=0,microsecond=0).time(),
                        key=f"tic_hora_visita_{tid}"
                    )
                    st.caption("✅ Ao salvar, esta visita também será incluída automaticamente na Agenda.")

            b1,b2,b3 = st.columns([1,1,1.8])
            with b1:
                anterior = st.button("⬅️ Anterior", key=f"tic_ant_{tid}", use_container_width=True)
            with b2:
                pular = st.button("⏭️ Pular", key=f"tic_pular_{tid}", use_container_width=True)
            with b3:
                salvar = st.button("💾 Salvar e próximo", key=f"tic_salvar_{tid}", type="primary", use_container_width=True)

            if anterior:
                st.session_state["ticlog_pos"] = (pos-1) % len(ativos_t)
                st.rerun()

            if pular:
                st.session_state["ticlog_pos"] = (pos+1) % len(ativos_t)
                st.rerun()

            if salvar:
                if not acao_t:
                    st.warning("Selecione a ação realizada/próxima ação.")
                elif not resultado_t:
                    st.warning("Selecione o resultado.")
                elif acao_t == "📅 Agendar visita" and data_visita_t is None:
                    st.warning("Informe a data da visita.")
                else:
                    registrar_acao_ticlog(
                        tid,
                        acao_t,
                        resultado_t,
                        obs_t,
                        data_visita_t,
                        hora_visita_t
                    )
                    st.session_state["ticlog_pos"] = min(pos, max(len(ativos_t)-1, 0))
                    st.success("Ação salva.")
                    st.rerun()

        st.divider()
        with st.expander("➕ Adicionar cliente TICLOG", expanded=False):
            with st.form("form_novo_ticlog", clear_on_submit=True):
                n_empresa = st.text_input("Empresa *")
                n_end = st.text_input("Endereço *")
                n_tel = st.text_input("Telefone")
                n_site = st.text_input("Site")
                n_salvar = st.form_submit_button("Adicionar cliente", type="primary", use_container_width=True)
            if n_salvar:
                if not n_empresa.strip() or not n_end.strip():
                    st.error("Informe empresa e endereço.")
                else:
                    ok,msg = salvar_cliente_ticlog_novo(n_empresa,n_end,n_tel,n_site)
                    (st.success if ok else st.warning)(msg)
                    if ok:
                        st.rerun()

        with st.expander("📋 Ver carteira completa / finalizados", expanded=False):
            visual = df_t.copy()
            if "data_visita" in visual.columns:
                visual["data_visita"] = pd.to_datetime(visual["data_visita"], errors="coerce").dt.strftime("%d/%m/%Y")
            cols = [c for c in ["empresa","endereco","telefone","site","status","total_tentativas","data_visita","hora_visita","ultima_observacao"] if c in visual.columns]
            st.dataframe(visual[cols], use_container_width=True, hide_index=True)

elif menu == "📅 Agenda":
    st.markdown("## 📅 Agenda")
    st.caption("Visitas, reuniões e compromissos comerciais em uma visão simples.")

    dados_ag = carregar_database(forcar_github=False)
    agenda = dados_ag.get("agenda", []) or []
    hoje_ag = date.today()

    agenda_df = pd.DataFrame(agenda)
    if not agenda_df.empty:
        agenda_df["data_dt"] = pd.to_datetime(agenda_df["data"], errors="coerce").dt.date
        agenda_df["horario_ord"] = agenda_df["horario"].fillna("")
        agenda_ativos = agenda_df[~agenda_df["status"].isin(["CANCELADO"])].copy()
    else:
        agenda_ativos = pd.DataFrame()

    cores_agenda = {
        "VISITA": "#e8f1ff",
        "VISITA TICLOG": "#dff7e8",
        "REUNIÃO": "#f3e8ff",
        "EVENTO": "#fff3d6",
        "RETORNO": "#e8fff2",
        "OUTRO": "#f2f4f7",
    }

    # Cards rápidos
    qtd_hoje = 0
    qtd_amanha = 0
    qtd_7d = 0
    if not agenda_ativos.empty:
        qtd_hoje = int((agenda_ativos["data_dt"] == hoje_ag).sum())
        qtd_amanha = int((agenda_ativos["data_dt"] == hoje_ag + timedelta(days=1)).sum())
        qtd_7d = int((
            (agenda_ativos["data_dt"] >= hoje_ag) &
            (agenda_ativos["data_dt"] <= hoje_ag + timedelta(days=6))
        ).sum())

    a1,a2,a3 = st.columns(3)
    a1.metric("📍 Hoje", qtd_hoje)
    a2.metric("🌤️ Amanhã", qtd_amanha)
    a3.metric("📆 Próximos 7 dias", qtd_7d)

    # Próximos compromissos em cards, mesmo que estejam a semanas/meses de distância.
    if not agenda_ativos.empty:
        proximos_cards = agenda_ativos[
            agenda_ativos["data_dt"] >= hoje_ag
        ].sort_values(["data_dt","horario_ord","id"]).head(4)
    else:
        proximos_cards = pd.DataFrame()

    st.markdown("### Próximos compromissos")
    if proximos_cards.empty:
        st.caption("Nenhum compromisso futuro programado.")
    else:
        colunas_cards = st.columns(min(4, len(proximos_cards)))
        for idx, (_, item) in enumerate(proximos_cards.iterrows()):
            data_card = item.get("data_dt")
            data_txt = data_card.strftime("%d/%m/%Y") if pd.notna(data_card) else "-"
            hora_txt = str(item.get("horario") or "--:--")
            tipo_txt = str(item.get("tipo") or "OUTRO").upper()
            titulo_txt = str(item.get("cliente_compromisso") or "Compromisso")
            local_txt = str(item.get("local") or "Local não informado")
            fundo = cores_agenda.get(tipo_txt, "#f2f4f7")
            with colunas_cards[idx]:
                st.markdown(
                    f"""
                    <div style="background:{fundo};border:1px solid #e3e7ee;border-radius:14px;
                                padding:.9rem;min-height:150px;">
                        <div style="font-size:.78rem;color:#667085;font-weight:700;">
                            {data_txt} • {hora_txt}
                        </div>
                        <div style="font-size:1rem;font-weight:800;margin-top:.35rem;">
                            {titulo_txt}
                        </div>
                        <div style="font-size:.82rem;color:#5f6878;margin-top:.35rem;">
                            {tipo_txt}
                        </div>
                        <div style="font-size:.82rem;color:#5f6878;margin-top:.15rem;">
                            📍 {local_txt}
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )

                eid_card = int(item["id"])
                cb1, cb2 = st.columns(2)
                with cb1:
                    if st.button("✏️ Editar", key=f"agenda_card_edit_{eid_card}", use_container_width=True):
                        st.session_state["agenda_editar_id"] = eid_card
                        st.session_state.pop("agenda_excluir_id", None)
                        st.rerun()
                with cb2:
                    if st.button("🗑️ Excluir", key=f"agenda_card_del_{eid_card}", use_container_width=True):
                        st.session_state["agenda_excluir_id"] = eid_card
                        st.session_state.pop("agenda_editar_id", None)
                        st.rerun()

    # Confirmação de exclusão
    excluir_id = st.session_state.get("agenda_excluir_id")
    if excluir_id:
        item_exc = next(
            (a for a in agenda if int(a.get("id", 0) or 0) == int(excluir_id)),
            None
        )
        if item_exc:
            st.warning(
                f"Excluir o compromisso **{item_exc.get('cliente_compromisso') or 'Compromisso'}** "
                f"de {pd.to_datetime(item_exc.get('data'), errors='coerce').strftime('%d/%m/%Y') if pd.notna(pd.to_datetime(item_exc.get('data'), errors='coerce')) else item_exc.get('data')}?"
            )
            ex1, ex2 = st.columns(2)
            with ex1:
                if st.button("✅ Sim, excluir", type="primary", key="agenda_confirmar_exclusao", use_container_width=True):
                    excluir_compromisso_agenda(int(excluir_id))
                    st.session_state.pop("agenda_excluir_id", None)
                    st.success("Compromisso excluído.")
                    st.rerun()
            with ex2:
                if st.button("Cancelar", key="agenda_cancelar_exclusao", use_container_width=True):
                    st.session_state.pop("agenda_excluir_id", None)
                    st.rerun()

    # Edição do compromisso selecionado
    editar_id = st.session_state.get("agenda_editar_id")
    if editar_id:
        item_ed = next(
            (a for a in agenda if int(a.get("id", 0) or 0) == int(editar_id)),
            None
        )
        if item_ed:
            st.markdown("### ✏️ Editar compromisso")
            data_ed_atual = pd.to_datetime(item_ed.get("data"), errors="coerce")
            data_ed_default = data_ed_atual.date() if pd.notna(data_ed_atual) else hoje_ag

            hora_txt_atual = str(item_ed.get("horario") or "09:00")
            try:
                hora_ed_default = datetime.strptime(hora_txt_atual, "%H:%M").time()
            except Exception:
                hora_ed_default = datetime.now().replace(second=0, microsecond=0).time()

            tipos_agenda = ["VISITA","VISITA TICLOG","REUNIÃO","EVENTO","RETORNO","OUTRO"]
            tipo_atual = str(item_ed.get("tipo") or "OUTRO").upper()
            if tipo_atual not in tipos_agenda:
                tipos_agenda.append(tipo_atual)

            status_agenda = ["PROGRAMADO","REALIZADO","CANCELADO"]
            status_atual = str(item_ed.get("status") or "PROGRAMADO").upper()
            if status_atual not in status_agenda:
                status_agenda.append(status_atual)

            with st.form(f"form_editar_agenda_{editar_id}"):
                e1,e2,e3 = st.columns([1,1,1.2])
                ed_data = e1.date_input("Data *", value=data_ed_default, format="DD/MM/YYYY")
                ed_hora = e2.time_input("Horário *", value=hora_ed_default)
                ed_tipo = e3.selectbox(
                    "Tipo *",
                    tipos_agenda,
                    index=tipos_agenda.index(tipo_atual)
                )
                ed_cliente = st.text_input(
                    "Cliente / Compromisso *",
                    value=str(item_ed.get("cliente_compromisso") or "")
                )
                e4,e5 = st.columns([1.2,1])
                ed_local = e4.text_input("Cidade / Local", value=str(item_ed.get("local") or ""))
                ed_status = e5.selectbox(
                    "Status",
                    status_agenda,
                    index=status_agenda.index(status_atual)
                )
                ed_obs = st.text_input("Observação", value=str(item_ed.get("observacao") or ""))

                ec1, ec2 = st.columns(2)
                salvar_ed = ec1.form_submit_button("💾 Salvar alterações", type="primary", use_container_width=True)
                cancelar_ed = ec2.form_submit_button("Cancelar edição", use_container_width=True)

            if salvar_ed:
                if not ed_cliente.strip():
                    st.error("Informe o cliente ou compromisso.")
                else:
                    atualizar_compromisso_agenda(
                        int(editar_id),
                        ed_data,
                        ed_hora,
                        ed_tipo,
                        ed_cliente,
                        ed_local,
                        ed_obs,
                        ed_status
                    )
                    st.session_state.pop("agenda_editar_id", None)
                    st.success("Compromisso atualizado.")
                    st.rerun()

            if cancelar_ed:
                st.session_state.pop("agenda_editar_id", None)
                st.rerun()

    st.markdown("### Compromissos de hoje")
    if agenda_ativos.empty:
        agenda_hoje = pd.DataFrame()
    else:
        agenda_hoje = agenda_ativos[
            agenda_ativos["data_dt"] == hoje_ag
        ].sort_values(["horario_ord","id"])

    if agenda_hoje.empty:
        st.info("Nenhum compromisso programado para hoje.")
    else:
        for _, item in agenda_hoje.iterrows():
            tipo_item = str(item.get("tipo") or "OUTRO").upper()
            fundo = cores_agenda.get(tipo_item, "#f2f4f7")
            horario = str(item.get("horario") or "--:--")
            titulo = str(item.get("cliente_compromisso") or "Compromisso")
            local = str(item.get("local") or "Local não informado")
            status_item = str(item.get("status") or "PROGRAMADO")
            st.markdown(
                f"""
                <div style="background:{fundo};border:1px solid #e3e7ee;border-radius:14px;
                            padding:.8rem 1rem;margin:.35rem 0;">
                    <div style="font-size:1.05rem;font-weight:800;">{horario} • {titulo}</div>
                    <div style="font-size:.86rem;color:#5f6878;margin-top:.2rem;">
                        {tipo_item} • {local} • {status_item}
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )
            if str(item.get("observacao") or "").strip():
                st.caption(f"📝 {item.get('observacao')}")

            c1,c2,c3 = st.columns(3)
            with c1:
                if status_item != "REALIZADO":
                    if st.button("✅ Realizado", key=f"agenda_realizado_{int(item['id'])}", use_container_width=True):
                        atualizar_status_agenda(int(item["id"]), "REALIZADO")
                        st.rerun()
            with c2:
                if st.button("✏️ Editar", key=f"agenda_hoje_edit_{int(item['id'])}", use_container_width=True):
                    st.session_state["agenda_editar_id"] = int(item["id"])
                    st.session_state.pop("agenda_excluir_id", None)
                    st.rerun()
            with c3:
                if st.button("🗑️ Excluir", key=f"agenda_hoje_del_{int(item['id'])}", use_container_width=True):
                    st.session_state["agenda_excluir_id"] = int(item["id"])
                    st.session_state.pop("agenda_editar_id", None)
                    st.rerun()

    with st.expander("➕ Novo compromisso", expanded=(qtd_hoje == 0 and len(agenda) == 0)):
        with st.form("novo_compromisso_agenda", clear_on_submit=True):
            c1,c2,c3 = st.columns([1,1,1.2])
            data_comp = c1.date_input("Data *", value=hoje_ag, format="DD/MM/YYYY")
            hora_comp = c2.time_input(
                "Horário *",
                value=datetime.now().replace(second=0, microsecond=0).time()
            )
            tipo_comp = c3.selectbox(
                "Tipo *",
                ["VISITA","REUNIÃO","EVENTO","RETORNO","OUTRO"]
            )
            cliente_comp = st.text_input(
                "Cliente / Compromisso *",
                placeholder="Ex.: Visita Della Via"
            )
            c4,c5 = st.columns([1.2,1])
            local_comp = c4.text_input("Cidade / Local", placeholder="Ex.: Campinas")
            obs_comp = c5.text_input("Observação", placeholder="Opcional")

            salvar_agenda = st.form_submit_button(
                "💾 Salvar compromisso",
                type="primary",
                use_container_width=True
            )

        if salvar_agenda:
            if not str(cliente_comp or "").strip():
                st.error("Informe o cliente ou compromisso.")
            else:
                salvar_compromisso_agenda(
                    data_comp,
                    hora_comp.strftime("%H:%M"),
                    tipo_comp,
                    cliente_comp,
                    local_comp,
                    obs_comp
                )
                st.success("Compromisso salvo na agenda.")
                st.rerun()

    with st.expander("📋 Próximos compromissos", expanded=False):
        if agenda_ativos.empty:
            st.info("Agenda vazia.")
        else:
            futuros = agenda_ativos[
                agenda_ativos["data_dt"] >= hoje_ag
            ].sort_values(["data_dt","horario_ord"])
            cols = ["data","horario","tipo","cliente_compromisso","local","status","observacao"]
            vis = futuros[cols].copy()
            vis["data"] = pd.to_datetime(vis["data"], errors="coerce").dt.strftime("%d/%m/%Y")
            vis.columns = ["Data","Horário","Tipo","Cliente / Compromisso","Local","Status","Observação"]
            st.dataframe(vis, use_container_width=True, hide_index=True)
            st.download_button(
                "⬇️ Exportar agenda",
                data=excel_bytes_dataframe(vis, "Agenda"),
                file_name=f"agenda_comercial_{date.today().strftime('%d-%m-%Y')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )

    # Histórico permanente: compromissos passados nunca somem da consulta.
    with st.expander("🕘 Histórico da agenda", expanded=False):
        if agenda_df.empty:
            st.info("Nenhum compromisso histórico.")
        else:
            passados = agenda_df[
                agenda_df["data_dt"] < hoje_ag
            ].copy()

            if passados.empty:
                st.caption("Ainda não há compromissos passados.")
            else:
                min_hist = passados["data_dt"].dropna().min()
                max_hist = passados["data_dt"].dropna().max()

                h1,h2,h3 = st.columns(3)
                hist_de = h1.date_input(
                    "De",
                    value=min_hist,
                    max_value=hoje_ag,
                    format="DD/MM/YYYY",
                    key="agenda_hist_de"
                )
                hist_ate = h2.date_input(
                    "Até",
                    value=max_hist,
                    max_value=hoje_ag,
                    format="DD/MM/YYYY",
                    key="agenda_hist_ate"
                )
                tipos_hist = sorted(passados["tipo"].dropna().astype(str).unique().tolist())
                tipo_hist = h3.selectbox(
                    "Tipo",
                    ["TODOS"] + tipos_hist,
                    key="agenda_hist_tipo"
                )

                hist_f = passados[
                    (passados["data_dt"] >= hist_de) &
                    (passados["data_dt"] <= hist_ate)
                ].copy()
                if tipo_hist != "TODOS":
                    hist_f = hist_f[hist_f["tipo"] == tipo_hist].copy()

                busca_hist = st.text_input(
                    "Pesquisar cliente/compromisso ou local",
                    key="agenda_hist_busca"
                ).strip().lower()
                if busca_hist:
                    hist_f = hist_f[
                        hist_f["cliente_compromisso"].fillna("").astype(str).str.lower().str.contains(busca_hist, regex=False)
                        | hist_f["local"].fillna("").astype(str).str.lower().str.contains(busca_hist, regex=False)
                    ]

                hist_f = hist_f.sort_values(["data_dt","horario_ord"], ascending=[False,False])

                vis_hist = hist_f[
                    ["id","data","horario","tipo","cliente_compromisso","local","status","observacao"]
                ].copy()
                vis_hist["data"] = pd.to_datetime(vis_hist["data"], errors="coerce").dt.strftime("%d/%m/%Y")
                vis_hist.columns = [
                    "ID","Data","Horário","Tipo","Cliente / Compromisso",
                    "Local","Status","Observação"
                ]
                st.dataframe(vis_hist, use_container_width=True, hide_index=True)

                if not hist_f.empty:
                    mapa_hist = {
                        f"{pd.to_datetime(r['data'], errors='coerce').strftime('%d/%m/%Y')} • "
                        f"{r.get('horario') or '--:--'} • {r.get('cliente_compromisso') or 'Compromisso'} "
                        f"[ID {int(r['id'])}]": int(r["id"])
                        for _, r in hist_f.iterrows()
                    }
                    selecionado_hist = st.selectbox(
                        "Selecionar compromisso histórico",
                        list(mapa_hist.keys()),
                        key="agenda_hist_selecao"
                    )
                    hc1,hc2 = st.columns(2)
                    with hc1:
                        if st.button("✏️ Editar histórico", key="agenda_hist_editar", use_container_width=True):
                            st.session_state["agenda_editar_id"] = mapa_hist[selecionado_hist]
                            st.session_state.pop("agenda_excluir_id", None)
                            st.rerun()
                    with hc2:
                        if st.button("🗑️ Excluir histórico", key="agenda_hist_excluir", use_container_width=True):
                            st.session_state["agenda_excluir_id"] = mapa_hist[selecionado_hist]
                            st.session_state.pop("agenda_editar_id", None)
                            st.rerun()

                st.download_button(
                    "⬇️ Exportar histórico da agenda",
                    data=excel_bytes_dataframe(vis_hist.drop(columns=["ID"], errors="ignore"), "Histórico Agenda"),
                    file_name=f"historico_agenda_ate_{date.today().strftime('%d-%m-%Y')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True
                )

# ---------------- VEÍCULO DA EMPRESA ----------------
elif menu == "🚗 Veículo da empresa":
    st.markdown("## 🚗 Veículo da empresa")
    st.caption("Registro rápido do uso do carro e análise de onde a quilometragem está sendo utilizada.")

    adicionados_hist = importar_historico_veiculo_se_necessario()
    if adicionados_hist > 0:
        st.success(f"✅ Histórico da planilha incorporado ao banco: {adicionados_hist} registro(s).")

    dados_v = carregar_database(forcar_github=False)
    registros_v = dados_v.get("veiculo_registros", []) or []
    tipos_v = dados_v.get("veiculo_tipos", {}) or {}

    for tipo, motivos in VEICULO_TIPOS_PADRAO.items():
        tipos_v.setdefault(tipo, motivos)

    # ---------------- REGISTRO SIMPLES ----------------
    st.markdown("### ➕ Registrar uso")

    placas_existentes = sorted({
        str(r.get("placa") or "").strip()
        for r in registros_v
        if str(r.get("placa") or "").strip()
    })
    placas_opcoes = placas_existentes + ["OUTRA PLACA"]

    r1,r2 = st.columns(2)
    data_v = r1.date_input("Data *", value=date.today(), format="DD/MM/YYYY", key="veic_data")
    placa_sel = r2.selectbox(
        "Placa *",
        placas_opcoes,
        index=max(len(placas_existentes)-1,0),
        key="veic_placa_sel"
    )
    placa_v = (
        st.text_input("Informe a placa", key="veic_placa_nova")
        if placa_sel == "OUTRA PLACA"
        else placa_sel
    )

    km_default = ultimo_km_final(registros_v, placa_v) if placa_v else None
    k1,k2 = st.columns(2)
    km_ini = k1.number_input(
        "KM inicial *",
        min_value=0,
        value=int(km_default or 0),
        step=1,
        key="veic_km_ini"
    )
    km_fim = k2.number_input(
        "KM final",
        min_value=0,
        value=int(km_default or 0),
        step=1,
        key="veic_km_fim"
    )
    if km_default is not None:
        st.caption(f"Último KM final cadastrado para esta placa: **{km_default}**")

    tipos_lista = sorted(tipos_v.keys())
    tipo_uso_v = st.pills(
        "Tipo de uso *",
        tipos_lista,
        selection_mode="single",
        key="veic_tipo_pills"
    )

    motivo_v = ""
    if tipo_uso_v:
        motivos_lista = sorted(set(list(tipos_v.get(tipo_uso_v, []) or []) + ["OUTRO"]))
        motivo_sel = st.pills(
            "Motivo / Situação *",
            motivos_lista,
            selection_mode="single",
            key=f"veic_motivo_pills_{tipo_uso_v}"
        )
        if motivo_sel == "OUTRO":
            motivo_v = st.text_input("Descreva o motivo", key="veic_motivo_outro")
        else:
            motivo_v = motivo_sel or ""

    cidade_v = st.text_input(
        "Cidade / Região",
        placeholder="Ex.: Campinas / SP",
        key="veic_cidade"
    )
    obs_v = st.text_area(
        "Observação (opcional)",
        height=65,
        key="veic_obs"
    )

    # Valores padrão dos campos opcionais
    cliente_v = ""
    endereco_v = ""
    hora_saida_v = None
    hora_retorno_v = None
    abasteceu_v = "NÃO"
    valor_abast_v = 0.0
    litros_v = 0.0
    combustivel_v = ""
    pedagio_v = 0.0
    estacionamento_v = 0.0
    outros_v = 0.0
    descricao_outros_v = ""

    copt1,copt2 = st.columns(2)
    with copt1:
        with st.expander("👤 Cliente / destino", expanded=False):
            cliente_v = st.text_input("Cliente", key="veic_cliente")
            endereco_v = st.text_input("Endereço / destino", key="veic_endereco")
            t1,t2 = st.columns(2)
            hora_saida_v = t1.time_input(
                "Hora de saída",
                value=datetime.now().replace(second=0,microsecond=0).time(),
                key="veic_h_saida"
            )
            hora_retorno_v = t2.time_input(
                "Hora de retorno",
                value=datetime.now().replace(second=0,microsecond=0).time(),
                key="veic_h_retorno"
            )

    with copt2:
        with st.expander("💰 Gastos / abastecimento", expanded=False):
            abasteceu_v = st.selectbox("Abasteceu?", ["NÃO","SIM"], key="veic_abasteceu")
            if abasteceu_v == "SIM":
                g1,g2,g3 = st.columns(3)
                valor_abast_v = g1.number_input("Valor (R$)", min_value=0.0, value=0.0, step=0.01, key="veic_valor_abast")
                litros_v = g2.number_input("Litros", min_value=0.0, value=0.0, step=0.01, key="veic_litros")
                combustivel_v = g3.selectbox("Combustível", ["","ETANOL","GASOLINA","DIESEL","OUTRO"], key="veic_comb")

            g4,g5,g6 = st.columns(3)
            pedagio_v = g4.number_input("Pedágio (R$)", min_value=0.0, value=0.0, step=0.01, key="veic_pedagio")
            estacionamento_v = g5.number_input("Estacionamento (R$)", min_value=0.0, value=0.0, step=0.01, key="veic_estac")
            outros_v = g6.number_input("Outros (R$)", min_value=0.0, value=0.0, step=0.01, key="veic_outros")
            if outros_v > 0:
                descricao_outros_v = st.text_input("Descrição do outro gasto", key="veic_desc_outros")

    if st.button("💾 Salvar uso do veículo", type="primary", use_container_width=True, key="veic_salvar"):
        erros_v = []
        if not str(placa_v or "").strip():
            erros_v.append("Informe a placa.")
        if km_fim and km_ini and km_fim < km_ini:
            erros_v.append("O KM final não pode ser menor que o KM inicial.")
        if not tipo_uso_v:
            erros_v.append("Selecione o tipo de uso.")
        if not str(motivo_v or "").strip():
            erros_v.append("Selecione ou informe o motivo.")

        if erros_v:
            for e in erros_v:
                st.error(e)
        else:
            registro = {
                "data": data_v.isoformat(),
                "placa": str(placa_v).strip().upper(),
                "km_inicial": int(km_ini) if km_ini is not None else None,
                "km_final": int(km_fim) if km_fim is not None else None,
                "tipo_uso": tipo_uso_v,
                "motivo": str(motivo_v).strip().upper(),
                "motivo_original": str(motivo_v).strip().upper(),
                "cliente": str(cliente_v or "").strip(),
                "endereco": str(endereco_v or "").strip(),
                "cidade_regiao": str(cidade_v or "").strip(),
                "observacoes": str(obs_v or "").strip(),
                "abasteceu": abasteceu_v,
                "valor_abastecido": float(valor_abast_v) if valor_abast_v else None,
                "litros_abastecidos": float(litros_v) if litros_v else None,
                "tipo_combustivel": combustivel_v or None,
                "pedagio": float(pedagio_v) if pedagio_v else None,
                "estacionamento": float(estacionamento_v) if estacionamento_v else None,
                "outros_gastos": float(outros_v) if outros_v else None,
                "descricao_outros_gastos": str(descricao_outros_v or "").strip(),
                "hora_saida": hora_saida_v.strftime("%H:%M") if hora_saida_v else None,
                "hora_retorno": hora_retorno_v.strftime("%H:%M") if hora_retorno_v else None,
            }
            salvar_registro_veiculo(registro)
            st.success("Uso do veículo salvo no banco permanente.")
            st.rerun()

    with st.expander("⚙️ Cadastrar novo tipo ou motivo", expanded=False):
        modo_cad = st.radio(
            "Adicionar",
            ["Novo motivo","Novo tipo"],
            horizontal=True,
            key="veic_cad_modo"
        )
        if modo_cad == "Novo motivo":
            tipo_existente = st.selectbox("Tipo", sorted(tipos_v.keys()), key="cad_mot_tipo")
            novo_motivo = st.text_input("Novo motivo", key="cad_mot_nome")
            if st.button("Adicionar motivo", key="cad_mot_salvar"):
                if not novo_motivo.strip():
                    st.warning("Informe o motivo.")
                else:
                    adicionar_tipo_motivo_veiculo(tipo_existente, novo_motivo)
                    st.success("Motivo adicionado.")
                    st.rerun()
        else:
            novo_tipo = st.text_input("Novo tipo", key="cad_tipo_nome")
            primeiro_motivo = st.text_input("Primeiro motivo (opcional)", key="cad_tipo_motivo")
            if st.button("Adicionar tipo", key="cad_tipo_salvar"):
                if not novo_tipo.strip():
                    st.warning("Informe o tipo.")
                else:
                    adicionar_tipo_motivo_veiculo(novo_tipo, primeiro_motivo)
                    st.success("Tipo adicionado.")
                    st.rerun()

    # ---------------- ANÁLISE ----------------
    st.divider()
    st.markdown("## 📊 Análise do uso do veículo")
    st.caption("Os gráficos mostram onde o carro está sendo mais utilizado no período selecionado.")

    rel = dataframe_relatorio_veiculo(registros_v)
    if rel.empty:
        st.info("Nenhum registro de veículo.")
    else:
        rel["Data_dt"] = pd.to_datetime(rel["Data"], errors="coerce").dt.date

        f1,f2,f3 = st.columns(3)
        data_minima = rel["Data_dt"].dropna().min()
        periodo_ini = f1.date_input(
            "De",
            value=max(data_minima, date.today()-timedelta(days=30)),
            format="DD/MM/YYYY",
            key="rel_veic_de"
        )
        periodo_fim = f2.date_input(
            "Até",
            value=date.today(),
            format="DD/MM/YYYY",
            key="rel_veic_ate"
        )
        placas_rel = sorted(rel["Placa do veículo"].dropna().astype(str).unique().tolist())
        placa_filtro = f3.selectbox("Placa", ["TODAS"] + placas_rel, key="rel_veic_placa")

        fil = rel[
            (rel["Data_dt"] >= periodo_ini) &
            (rel["Data_dt"] <= periodo_fim)
        ].copy()
        if placa_filtro != "TODAS":
            fil = fil[fil["Placa do veículo"] == placa_filtro].copy()

        km_num = pd.to_numeric(fil["KM Rodado"], errors="coerce")
        km_validos = km_num.where(km_num >= 0)
        anomalias_km = int((km_num < 0).sum())
        gastos_total = pd.to_numeric(
            fil["Total de gastos (R$)"],
            errors="coerce"
        ).fillna(0)

        m1,m2,m3,m4 = st.columns(4)
        m1.metric("Registros", len(fil))
        m2.metric("KM válidos", f"{km_validos.sum():,.0f}".replace(",", ".") if not fil.empty else "0")
        m3.metric("Gastos", f"R$ {gastos_total.sum():,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
        m4.metric("Inconsistências KM", anomalias_km)

        if anomalias_km:
            st.caption(
                f"⚠️ {anomalias_km} registro(s) histórico(s) têm KM final menor que KM inicial. "
                "Eles foram preservados no relatório, mas não entram nos gráficos de KM."
            )

        if not fil.empty:
            analise = fil.copy()
            analise["KM_analise"] = pd.to_numeric(analise["KM Rodado"], errors="coerce")
            analise.loc[analise["KM_analise"] < 0, "KM_analise"] = None

            por_tipo = analise.groupby("Tipo de uso", dropna=False).agg(
                Registros=("Tipo de uso","size"),
                KM=("KM_analise","sum")
            ).reset_index()
            por_tipo["Tipo de uso"] = por_tipo["Tipo de uso"].fillna("SEM CLASSIFICAÇÃO")
            por_tipo["KM"] = por_tipo["KM"].fillna(0)

            # 1) Quantidade de usos por tipo
            st.markdown("### Quantidade de usos por tipo")
            por_tipo["Uso_label"] = por_tipo["Registros"].astype(int).astype(str) + " usos"

            base_usos = alt.Chart(por_tipo).encode(
                y=alt.Y(
                    "Tipo de uso:N",
                    sort="-x",
                    title=None,
                    axis=alt.Axis(labelLimit=230)
                ),
                x=alt.X(
                    "Registros:Q",
                    title="Quantidade de usos",
                    axis=alt.Axis(format="d")
                ),
                color=alt.Color(
                    "Tipo de uso:N",
                    title="Tipo de uso",
                    scale=alt.Scale(scheme="tableau10"),
                    legend=alt.Legend(orient="bottom")
                ),
                tooltip=[
                    "Tipo de uso:N",
                    alt.Tooltip("Registros:Q", format="d", title="Usos"),
                    alt.Tooltip("KM:Q", format=".0f", title="KM")
                ]
            )
            barras_usos = base_usos.mark_bar(cornerRadiusEnd=6)
            labels_usos = alt.Chart(por_tipo).mark_text(
                align="left",
                dx=6,
                fontWeight="bold"
            ).encode(
                y=alt.Y("Tipo de uso:N", sort="-x"),
                x="Registros:Q",
                text="Uso_label:N"
            )
            st.altair_chart(
                (barras_usos + labels_usos).properties(
                    height=max(280, len(por_tipo) * 38)
                ),
                use_container_width=True
            )

            # 2) Relação Uso x KM
            st.markdown("### Uso x KM")
            st.caption(
                "Compara quantas vezes o veículo foi utilizado com a quilometragem rodada em cada tipo de uso."
            )

            por_tipo["KM_label"] = por_tipo["KM"].round(0).astype(int).astype(str) + " km"
            por_tipo["Comparativo"] = (
                por_tipo["Registros"].astype(int).astype(str)
                + " usos • "
                + por_tipo["KM_label"]
            )

            pontos = alt.Chart(por_tipo).mark_circle(
                size=260,
                opacity=0.85
            ).encode(
                x=alt.X(
                    "Registros:Q",
                    title="Quantidade de usos",
                    axis=alt.Axis(format="d")
                ),
                y=alt.Y(
                    "KM:Q",
                    title="KM rodados",
                    axis=alt.Axis(format=".0f")
                ),
                color=alt.Color(
                    "Tipo de uso:N",
                    title="Tipo de uso",
                    scale=alt.Scale(scheme="tableau10"),
                    legend=alt.Legend(orient="bottom")
                ),
                tooltip=[
                    "Tipo de uso:N",
                    alt.Tooltip("Registros:Q", format="d", title="Usos"),
                    alt.Tooltip("KM:Q", format=".0f", title="KM rodados")
                ]
            )

            textos = alt.Chart(por_tipo).mark_text(
                align="left",
                dx=10,
                dy=-8,
                fontWeight="bold"
            ).encode(
                x="Registros:Q",
                y="KM:Q",
                text="Comparativo:N"
            )

            st.altair_chart(
                (pontos + textos).properties(height=420),
                use_container_width=True
            )

            cga, cgb = st.columns([1,1])
            with cga:
                st.markdown("### Participação das saídas")
                donut = alt.Chart(por_tipo).mark_arc(innerRadius=55).encode(
                    theta=alt.Theta("Registros:Q"),
                    color=alt.Color(
                        "Tipo de uso:N",
                        title="Tipo de uso",
                        scale=alt.Scale(scheme="tableau10"),
                        legend=alt.Legend(orient="bottom")
                    ),
                    tooltip=[
                        "Tipo de uso:N",
                        alt.Tooltip("Registros:Q", format="d", title="Saídas")
                    ]
                ).properties(height=340)
                st.altair_chart(donut, use_container_width=True)

            with cgb:
                st.markdown("### Ranking dos motivos")
                motivos_rank = analise.groupby(
                    ["Tipo de uso","Motivo / Situação"],
                    dropna=False
                ).agg(
                    Saídas=("Motivo / Situação","size"),
                    KM=("KM_analise","sum")
                ).reset_index()
                motivos_rank["KM"] = motivos_rank["KM"].fillna(0)
                motivos_rank = motivos_rank.sort_values(
                    ["Saídas","KM"],
                    ascending=[False,False]
                ).head(12)
                st.dataframe(
                    motivos_rank.rename(columns={"Saídas":"Qtd. saídas"}),
                    use_container_width=True,
                    hide_index=True
                )

        with st.expander("📋 Ver registros e exportar", expanded=False):
            mostrar = fil.drop(columns=["Data_dt"], errors="ignore")
            st.dataframe(
                mostrar.tail(150).iloc[::-1],
                use_container_width=True,
                hide_index=True
            )

            # Edição/exclusão de registro salvo.
            registros_periodo = []
            for r in registros_v:
                dt_r = pd.to_datetime(r.get("data"), errors="coerce")
                if pd.isna(dt_r):
                    continue
                d_r = dt_r.date()
                if not (periodo_ini <= d_r <= periodo_fim):
                    continue
                if placa_filtro != "TODAS" and str(r.get("placa") or "") != placa_filtro:
                    continue
                registros_periodo.append(r)

            if registros_periodo:
                registros_periodo = sorted(
                    registros_periodo,
                    key=lambda r: (str(r.get("data") or ""), int(r.get("id",0) or 0)),
                    reverse=True
                )
                mapa_reg = {
                    f"{pd.to_datetime(r.get('data'), errors='coerce').strftime('%d/%m/%Y')} • "
                    f"{r.get('tipo_uso') or '-'} • {r.get('motivo') or '-'} • "
                    f"KM {r.get('km_inicial') if r.get('km_inicial') is not None else '-'}"
                    f"→{r.get('km_final') if r.get('km_final') is not None else '-'} "
                    f"[ID {int(r.get('id',0) or 0)}]": int(r.get("id",0) or 0)
                    for r in registros_periodo
                }

                st.markdown("#### ✏️ Editar ou excluir um registro")
                escolha_reg = st.selectbox(
                    "Registro",
                    list(mapa_reg.keys()),
                    key="veic_registro_edicao"
                )
                rid = mapa_reg[escolha_reg]
                registro_ed = next(
                    (r for r in registros_v if int(r.get("id",0) or 0) == rid),
                    None
                )

                vr1,vr2 = st.columns(2)
                with vr1:
                    if st.button("✏️ Editar registro", key="veic_btn_editar_reg", use_container_width=True):
                        st.session_state["veic_editar_id"] = rid
                        st.session_state.pop("veic_excluir_id", None)
                        st.rerun()
                with vr2:
                    if st.button("🗑️ Excluir registro", key="veic_btn_excluir_reg", use_container_width=True):
                        st.session_state["veic_excluir_id"] = rid
                        st.session_state.pop("veic_editar_id", None)
                        st.rerun()

                excluir_vid = st.session_state.get("veic_excluir_id")
                if excluir_vid and registro_ed and int(excluir_vid) == rid:
                    st.warning(
                        "Tem certeza que deseja excluir este registro de uso do veículo? "
                        "A exclusão remove somente este lançamento."
                    )
                    vx1,vx2 = st.columns(2)
                    with vx1:
                        if st.button("✅ Confirmar exclusão", key="veic_confirmar_exclusao", type="primary", use_container_width=True):
                            excluir_registro_veiculo(rid)
                            st.session_state.pop("veic_excluir_id", None)
                            st.success("Registro excluído.")
                            st.rerun()
                    with vx2:
                        if st.button("Cancelar", key="veic_cancelar_exclusao", use_container_width=True):
                            st.session_state.pop("veic_excluir_id", None)
                            st.rerun()

                editar_vid = st.session_state.get("veic_editar_id")
                if editar_vid and registro_ed and int(editar_vid) == rid:
                    if str(registro_ed.get("origem") or "").upper().startswith("PLANILHA"):
                        st.info("ℹ️ Este registro veio da planilha histórica. A edição é permitida, mas a origem será preservada.")

                    data_default = pd.to_datetime(registro_ed.get("data"), errors="coerce")
                    data_default = data_default.date() if pd.notna(data_default) else date.today()

                    tipos_edit = sorted(set(list(tipos_v.keys()) + [str(registro_ed.get("tipo_uso") or "OUTROS / CORPORATIVO")]))
                    tipo_atual = str(registro_ed.get("tipo_uso") or tipos_edit[0])

                    with st.form(f"form_editar_veiculo_{rid}"):
                        ev1,ev2,ev3 = st.columns(3)
                        ev_data = ev1.date_input("Data", value=data_default, format="DD/MM/YYYY")
                        ev_placa = ev2.text_input("Placa", value=str(registro_ed.get("placa") or ""))
                        ev_tipo = ev3.selectbox("Tipo de uso", tipos_edit, index=tipos_edit.index(tipo_atual))

                        ev4,ev5 = st.columns(2)
                        ev_kmi = ev4.number_input(
                            "KM inicial",
                            min_value=0,
                            value=int(float(registro_ed.get("km_inicial") or 0)),
                            step=1
                        )
                        ev_kmf = ev5.number_input(
                            "KM final",
                            min_value=0,
                            value=int(float(registro_ed.get("km_final") or 0)),
                            step=1
                        )

                        motivos_edit = sorted(set(
                            list(tipos_v.get(ev_tipo, []) or [])
                            + [str(registro_ed.get("motivo") or "OUTRO"), "OUTRO"]
                        ))
                        motivo_atual = str(registro_ed.get("motivo") or "OUTRO")
                        if motivo_atual not in motivos_edit:
                            motivos_edit.append(motivo_atual)
                        ev_motivo_sel = st.selectbox(
                            "Motivo / Situação",
                            motivos_edit,
                            index=motivos_edit.index(motivo_atual)
                        )
                        ev_motivo = (
                            st.text_input("Descreva o motivo", value=motivo_atual)
                            if ev_motivo_sel == "OUTRO"
                            else ev_motivo_sel
                        )

                        ev_cidade = st.text_input("Cidade / Região", value=str(registro_ed.get("cidade_regiao") or ""))
                        ev_obs = st.text_area("Observação", value=str(registro_ed.get("observacoes") or ""), height=70)

                        with st.expander("👤 Cliente / destino", expanded=False):
                            ev_cliente = st.text_input("Cliente", value=str(registro_ed.get("cliente") or ""))
                            ev_endereco = st.text_input("Endereço / destino", value=str(registro_ed.get("endereco") or ""))

                        with st.expander("💰 Gastos / abastecimento", expanded=False):
                            abasteceu_atual = str(registro_ed.get("abasteceu") or "NÃO").upper()
                            if abasteceu_atual not in ["NÃO","SIM"]:
                                abasteceu_atual = "NÃO"
                            ev_abasteceu = st.selectbox("Abasteceu?", ["NÃO","SIM"], index=["NÃO","SIM"].index(abasteceu_atual))
                            eg1,eg2 = st.columns(2)
                            ev_valor = eg1.number_input("Valor abastecido (R$)", min_value=0.0, value=float(registro_ed.get("valor_abastecido") or 0.0), step=0.01)
                            ev_litros = eg2.number_input("Litros", min_value=0.0, value=float(registro_ed.get("litros_abastecidos") or 0.0), step=0.01)
                            eg3,eg4,eg5 = st.columns(3)
                            ev_pedagio = eg3.number_input("Pedágio (R$)", min_value=0.0, value=float(registro_ed.get("pedagio") or 0.0), step=0.01)
                            ev_estac = eg4.number_input("Estacionamento (R$)", min_value=0.0, value=float(registro_ed.get("estacionamento") or 0.0), step=0.01)
                            ev_outros = eg5.number_input("Outros gastos (R$)", min_value=0.0, value=float(registro_ed.get("outros_gastos") or 0.0), step=0.01)
                            ev_desc_outros = st.text_input("Descrição dos outros gastos", value=str(registro_ed.get("descricao_outros_gastos") or ""))

                        es1,es2 = st.columns(2)
                        salvar_ev = es1.form_submit_button("💾 Salvar alterações", type="primary", use_container_width=True)
                        cancelar_ev = es2.form_submit_button("Cancelar edição", use_container_width=True)

                    if salvar_ev:
                        if ev_kmf and ev_kmi and ev_kmf < ev_kmi:
                            st.error("O KM final não pode ser menor que o KM inicial.")
                        elif not ev_tipo or not str(ev_motivo or "").strip():
                            st.error("Informe tipo de uso e motivo.")
                        else:
                            atualizar_registro_veiculo(
                                rid,
                                {
                                    "data": ev_data.isoformat(),
                                    "placa": ev_placa.strip().upper(),
                                    "km_inicial": int(ev_kmi),
                                    "km_final": int(ev_kmf),
                                    "tipo_uso": ev_tipo,
                                    "motivo": str(ev_motivo).strip().upper(),
                                    "motivo_original": str(ev_motivo).strip().upper(),
                                    "cliente": ev_cliente.strip(),
                                    "endereco": ev_endereco.strip(),
                                    "cidade_regiao": ev_cidade.strip(),
                                    "observacoes": ev_obs.strip(),
                                    "abasteceu": ev_abasteceu,
                                    "valor_abastecido": float(ev_valor) if ev_valor else None,
                                    "litros_abastecidos": float(ev_litros) if ev_litros else None,
                                    "pedagio": float(ev_pedagio) if ev_pedagio else None,
                                    "estacionamento": float(ev_estac) if ev_estac else None,
                                    "outros_gastos": float(ev_outros) if ev_outros else None,
                                    "descricao_outros_gastos": ev_desc_outros.strip(),
                                }
                            )
                            st.session_state.pop("veic_editar_id", None)
                            st.success("Registro atualizado.")
                            st.rerun()

                    if cancelar_ev:
                        st.session_state.pop("veic_editar_id", None)
                        st.rerun()

            st.download_button(
                "⬇️ Exportar relatório em Excel",
                data=excel_bytes_dataframe(mostrar, "Uso do Veículo"),
                file_name=f"relatorio_veiculo_{periodo_ini.strftime('%d-%m-%Y')}_a_{periodo_fim.strftime('%d-%m-%Y')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )

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

st.sidebar.caption("Gestão Comercial • PERSISTENTE V13 • Integração TICLOG")

