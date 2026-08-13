
import streamlit as st
import pandas as pd
import sqlite3
import re
import io
import altair as alt
import base64
import requests
import threading
from pathlib import Path
from datetime import date, datetime, timedelta

st.set_page_config(page_title="Gestão Comercial", page_icon="📈", layout="wide")

DB_PATH = Path("database/comercial.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
ARQUIVO_INICIAL = Path("celso comercial.xlsx")

GITHUB_LOCK = threading.RLock()

def github_config():
    """Lê as credenciais do Streamlit Secrets. Em uso local, a sincronização é opcional."""
    try:
        token = str(st.secrets.get("GITHUB_TOKEN", "")).strip()
        repo = str(st.secrets.get("GITHUB_REPO", "")).strip()
        app_branch = str(st.secrets.get("GITHUB_APP_BRANCH", "main")).strip() or "main"
        data_branch = str(st.secrets.get("GITHUB_DATA_BRANCH", "database")).strip() or "database"
        db_repo_path = str(st.secrets.get("GITHUB_DB_PATH", "database/comercial.db")).strip() or "database/comercial.db"
    except Exception:
        token = repo = ""
        app_branch = "main"
        data_branch = "database"
        db_repo_path = "database/comercial.db"
    return token, repo, app_branch, data_branch, db_repo_path

def github_ativo():
    token, repo, *_ = github_config()
    return bool(token and repo)

def github_headers():
    token, *_ = github_config()
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def garantir_branch_dados():
    """Cria a branch de dados a partir da branch do app, se ainda não existir."""
    if not github_ativo():
        return False, "GitHub não configurado."

    token, repo, app_branch, data_branch, _ = github_config()
    headers = github_headers()

    ref_data = requests.get(
        f"https://api.github.com/repos/{repo}/git/ref/heads/{data_branch}",
        headers=headers, timeout=20
    )
    if ref_data.status_code == 200:
        return True, "Branch de dados disponível."

    ref_app = requests.get(
        f"https://api.github.com/repos/{repo}/git/ref/heads/{app_branch}",
        headers=headers, timeout=20
    )
    if ref_app.status_code != 200:
        return False, f"Não foi possível localizar a branch {app_branch}."

    sha = ref_app.json()["object"]["sha"]
    criar = requests.post(
        f"https://api.github.com/repos/{repo}/git/refs",
        headers=headers,
        json={"ref": f"refs/heads/{data_branch}", "sha": sha},
        timeout=20
    )
    if criar.status_code in (200, 201):
        return True, "Branch de dados criada."
    return False, f"Falha ao criar branch de dados: {criar.status_code}"

def _github_get_file(path_repo):
    token, repo, _, data_branch, _ = github_config()
    r = requests.get(
        f"https://api.github.com/repos/{repo}/contents/{path_repo}",
        headers=github_headers(),
        params={"ref": data_branch},
        timeout=30
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()

def _github_download_db_sem_lock():
    if not github_ativo():
        return False, "Sincronização GitHub não configurada."

    ok, msg = garantir_branch_dados()
    if not ok:
        return False, msg

    _, _, _, _, db_repo_path = github_config()
    info = _github_get_file(db_repo_path)
    if not info:
        return False, "A base ainda não existe na branch de dados."

    conteudo = info.get("content")
    if conteudo:
        dados = base64.b64decode(conteudo)
    else:
        download_url = info.get("download_url")
        if not download_url:
            return False, "Não foi possível obter o conteúdo da base."
        r = requests.get(download_url, headers=github_headers(), timeout=30)
        r.raise_for_status()
        dados = r.content

    tmp = DB_PATH.with_suffix(".download")
    tmp.write_bytes(dados)

    # Valida se o arquivo recebido é realmente SQLite antes de substituir.
    try:
        con = sqlite3.connect(tmp)
        con.execute("PRAGMA schema_version").fetchone()
        con.close()
    except Exception:
        tmp.unlink(missing_ok=True)
        return False, "A base recebida do GitHub não é um SQLite válido."

    tmp.replace(DB_PATH)
    return True, "Base carregada do GitHub com sucesso."

def carregar_base_github():
    with GITHUB_LOCK:
        return _github_download_db_sem_lock()

def _github_upload_file_sem_lock(local_path, repo_path, mensagem):
    if not github_ativo():
        return False, "Sincronização GitHub não configurada."

    ok, msg = garantir_branch_dados()
    if not ok:
        return False, msg

    token, repo, _, data_branch, _ = github_config()
    headers = github_headers()

    info = _github_get_file(repo_path)
    sha = info.get("sha") if info else None

    payload = {
        "message": mensagem,
        "content": base64.b64encode(Path(local_path).read_bytes()).decode("ascii"),
        "branch": data_branch,
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(
        f"https://api.github.com/repos/{repo}/contents/{repo_path}",
        headers=headers,
        json=payload,
        timeout=45
    )
    if r.status_code not in (200, 201):
        return False, f"Falha ao salvar no GitHub ({r.status_code})."
    return True, "Base salva no GitHub."

def salvar_base_github():
    """Atualiza a base oficial e um backup diário na mesma branch de dados."""
    with GITHUB_LOCK:
        if not github_ativo():
            return False, "Modo local: dados salvos apenas neste computador."

        _, _, _, _, db_repo_path = github_config()
        ok, msg = _github_upload_file_sem_lock(
            DB_PATH, db_repo_path, "Atualiza base comercial"
        )
        if not ok:
            return False, msg

        backup_path = f"database/backups/comercial_{date.today().strftime('%Y-%m-%d')}.db"
        _github_upload_file_sem_lock(
            DB_PATH, backup_path, f"Backup comercial {date.today().strftime('%d/%m/%Y')}"
        )
        return True, "Base sincronizada e backup diário atualizado."

def sincronizar_antes_de_gravar():
    """Busca a versão mais recente antes de escrever, reduzindo risco de sobrescrever dados."""
    if github_ativo():
        with GITHUB_LOCK:
            ok, _ = _github_download_db_sem_lock()
            return ok
    return True

# -----------------------------
# REGRAS COMERCIAIS
# -----------------------------
RESULTADOS = [
    "CONTATO REALIZADO",
    "SEM RETORNO",
    "AGUARDANDO CLIENTE",
    "RETORNO SOLICITADO",
    "REUNIÃO AGENDADA",
    "PROPOSTA SOLICITADA",
    "PROPOSTA ENVIADA",
    "EM NEGOCIAÇÃO",
    "FECHADO",
    "SEM INTERESSE",
    "SEM SUCESSO NO CONTATO",
    "NÃO UTILIZA TRANSPORTE",
    "JÁ UTILIZA AZUL",
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
    "TENTATIVA DE CONTATO",
    "AGUARDANDO CLIENTE",
    "RETORNO PENDENTE",
    "EM ANDAMENTO",
    "REUNIÃO AGENDADA",
    "PROPOSTA ENVIADA",
    "NEGOCIAÇÃO",
    "SEM RETORNO",
    "SEM SUCESSO NO CONTATO",
]

STATUS_ENCERRADOS = [
    "FECHADO",
    "SEM INTERESSE",
    "NÃO UTILIZA TRANSPORTE",
    "JÁ UTILIZA AZUL",
]

RESULTADOS_AGUARDANDO = {
    "SEM RETORNO",
    "AGUARDANDO CLIENTE",
    "RETORNO SOLICITADO",
    "SEM SUCESSO NO CONTATO",
}

MAPA_STATUS = {
    "CONTATO REALIZADO": "EM ANDAMENTO",
    "SEM RETORNO": "SEM RETORNO",
    "AGUARDANDO CLIENTE": "AGUARDANDO CLIENTE",
    "RETORNO SOLICITADO": "RETORNO PENDENTE",
    "REUNIÃO AGENDADA": "REUNIÃO AGENDADA",
    "PROPOSTA SOLICITADA": "EM ANDAMENTO",
    "PROPOSTA ENVIADA": "PROPOSTA ENVIADA",
    "EM NEGOCIAÇÃO": "NEGOCIAÇÃO",
    "FECHADO": "FECHADO",
    "SEM INTERESSE": "SEM INTERESSE",
    "SEM SUCESSO NO CONTATO": "SEM SUCESSO NO CONTATO",
    "NÃO UTILIZA TRANSPORTE": "NÃO UTILIZA TRANSPORTE",
    "JÁ UTILIZA AZUL": "JÁ UTILIZA AZUL",
    "OUTRO": "EM ANDAMENTO",
}

# -----------------------------
# BANCO
# -----------------------------
def conectar():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def coluna_existe(con, tabela, coluna):
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({tabela})").fetchall()]
    return coluna in cols

def criar_banco():
    with conectar() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS empresas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                documento TEXT,
                nome TEXT NOT NULL,
                telefone1 TEXT,
                telefone2 TEXT,
                telefone3 TEXT,
                status TEXT DEFAULT 'SEM CONTATO',
                observacao_atual TEXT,
                data_primeiro_contato TEXT,
                criado_em TEXT NOT NULL,
                origem TEXT DEFAULT 'APP'
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS contatos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                empresa_id INTEGER NOT NULL,
                data_contato TEXT NOT NULL,
                tipo_contato TEXT NOT NULL,
                resultado TEXT,
                status_novo TEXT,
                observacao TEXT,
                proxima_acao TEXT,
                data_proxima_acao TEXT,
                criado_em TEXT NOT NULL,
                FOREIGN KEY (empresa_id) REFERENCES empresas(id)
            )
        """)

        # Migração segura para quem já testou V1/V2.
        alteracoes_empresas = {
            "retorno_apos_seq": "INTEGER",
            "data_agendamento": "TEXT",
            "agendamento_pendente": "INTEGER DEFAULT 0",
            "proxima_acao": "TEXT",
        }
        for col, tipo in alteracoes_empresas.items():
            if not coluna_existe(con, "empresas", col):
                con.execute(f"ALTER TABLE empresas ADD COLUMN {col} {tipo}")

        if not coluna_existe(con, "contatos", "seq_global"):
            con.execute("ALTER TABLE contatos ADD COLUMN seq_global INTEGER")

        con.execute("CREATE INDEX IF NOT EXISTS idx_empresas_nome ON empresas(nome)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_empresas_documento ON empresas(documento)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_contatos_data ON contatos(data_contato)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_contatos_seq ON contatos(seq_global)")

def proxima_seq_global(con):
    atual = con.execute("SELECT COALESCE(MAX(seq_global),0) FROM contatos").fetchone()[0]
    return int(atual or 0) + 1

def seq_global_atual():
    with conectar() as con:
        return int(con.execute("SELECT COALESCE(MAX(seq_global),0) FROM contatos").fetchone()[0] or 0)

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
    if not ARQUIVO_INICIAL.exists():
        return

    with conectar() as con:
        if con.execute("SELECT COUNT(*) FROM empresas").fetchone()[0] > 0:
            return

    df = pd.read_excel(ARQUIVO_INICIAL, dtype=object)
    df.columns = [str(c).strip().upper() for c in df.columns]
    agora = datetime.now().isoformat(timespec="seconds")
    registros = []

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
        registros.append((
            documento, nome, tel1, tel2, tel3, status, obs,
            data1, agora, "PLANILHA INICIAL", None, None, 0, ""
        ))

    with conectar() as con:
        con.executemany("""
            INSERT INTO empresas
            (documento,nome,telefone1,telefone2,telefone3,status,observacao_atual,
             data_primeiro_contato,criado_em,origem,retorno_apos_seq,
             data_agendamento,agendamento_pendente,proxima_acao)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, registros)

        # Histórico importado não entra nos KPIs/tentativas da operação nova.
        empresas_hist = con.execute("""
            SELECT id, data_primeiro_contato, status, observacao_atual
            FROM empresas WHERE data_primeiro_contato IS NOT NULL
        """).fetchall()

        for e in empresas_hist:
            con.execute("""
                INSERT INTO contatos
                (empresa_id,data_contato,tipo_contato,resultado,status_novo,
                 observacao,proxima_acao,data_proxima_acao,criado_em,seq_global)
                VALUES (?,?,?,?,?,?,?,?,?,NULL)
            """, (
                e["id"], e["data_primeiro_contato"], "HISTÓRICO IMPORTADO",
                "CONTATO REGISTRADO NA PLANILHA", e["status"],
                e["observacao_atual"], "", None, agora
            ))

# -----------------------------
# DADOS
# -----------------------------
def carregar_empresas():
    with conectar() as con:
        return pd.read_sql_query("SELECT * FROM empresas ORDER BY nome", con)

def carregar_contatos():
    with conectar() as con:
        return pd.read_sql_query("""
            SELECT c.*, e.nome, e.documento, e.telefone1, e.telefone2, e.telefone3
            FROM contatos c
            JOIN empresas e ON e.id=c.empresa_id
            ORDER BY COALESCE(c.seq_global,0) DESC, c.id DESC
        """, con)

def tentativas_empresa(empresa_id):
    with conectar() as con:
        # Contamos as tentativas operacionais reais, excluindo histórico importado.
        return int(con.execute("""
            SELECT COUNT(*) FROM contatos
            WHERE empresa_id=? AND tipo_contato <> 'HISTÓRICO IMPORTADO'
        """, (empresa_id,)).fetchone()[0] or 0)

def salvar_empresa(documento, nome, telefones, status="SEM CONTATO", obs="", origem="APP"):
    with GITHUB_LOCK:
        sincronizar_antes_de_gravar()
        with conectar() as con:
            con.execute("""
                INSERT INTO empresas
                (documento,nome,telefone1,telefone2,telefone3,status,
                 observacao_atual,criado_em,origem,agendamento_pendente)
                VALUES (?,?,?,?,?,?,?,?,?,0)
            """, (
                formatar_documento(documento),
                nome.strip().upper(),
                formatar_telefone(telefones[0] if len(telefones)>0 else ""),
                formatar_telefone(telefones[1] if len(telefones)>1 else ""),
                formatar_telefone(telefones[2] if len(telefones)>2 else ""),
                status, obs.strip(),
                datetime.now().isoformat(timespec="seconds"), origem
            ))
        salvar_base_github()

def registrar_contato(empresa_id, data_contato, tipo, resultado, obs,
                      proxima_acao="", data_agendamento=None):
    """
    Regra:
    - Resultado define o status automaticamente.
    - Se resultado for de espera e NÃO houver data específica:
      volta depois de 20 novos contatos.
    - Na 3ª tentativa sem retorno/aguardando: SEM INTERESSE.
    - Se houver data específica: vira pendência agendada e permanece até nova atualização.
    """
    with GITHUB_LOCK:
        sincronizar_antes_de_gravar()
        agora = datetime.now().isoformat(timespec="seconds")

        with conectar() as con:
            tentativas_anteriores = int(con.execute("""
                SELECT COUNT(*) FROM contatos
                WHERE empresa_id=? AND tipo_contato <> 'HISTÓRICO IMPORTADO'
            """, (empresa_id,)).fetchone()[0] or 0)
            tentativa_atual = tentativas_anteriores + 1

            seq = proxima_seq_global(con)
            status_novo = MAPA_STATUS.get(resultado, "EM ANDAMENTO")

            retorno_apos = None
            agendamento_pendente = 0
            data_agendamento_iso = None

            if data_agendamento:
                agendamento_pendente = 1
                data_agendamento_iso = data_agendamento.isoformat()
            elif resultado in RESULTADOS_AGUARDANDO:
                if tentativa_atual >= 3:
                    status_novo = "SEM INTERESSE"
                else:
                    retorno_apos = seq + 20

            con.execute("""
                INSERT INTO contatos
                (empresa_id,data_contato,tipo_contato,resultado,status_novo,observacao,
                 proxima_acao,data_proxima_acao,criado_em,seq_global)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                empresa_id, data_contato.isoformat(), tipo, resultado, status_novo,
                obs.strip(), proxima_acao.strip(),
                data_agendamento_iso, agora, seq
            ))

            con.execute("""
                UPDATE empresas
                SET status=?,
                    observacao_atual=?,
                    data_primeiro_contato=COALESCE(data_primeiro_contato, ?),
                    retorno_apos_seq=?,
                    data_agendamento=?,
                    agendamento_pendente=?,
                    proxima_acao=?
                WHERE id=?
            """, (
                status_novo, obs.strip(), data_contato.isoformat(),
                retorno_apos, data_agendamento_iso,
                agendamento_pendente, proxima_acao.strip(), empresa_id
            ))

        ok_sync, msg_sync = salvar_base_github()
        if github_ativo() and not ok_sync:
            st.error(
                "O contato foi salvo localmente, mas a sincronização com o GitHub falhou. "
                "Use 'Carregar base de dados' antes de continuar e confira a configuração."
            )

        return status_novo, tentativa_atual, retorno_apos

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

    hist["Data"] = hist["data_contato"].apply(data_br)
    hist["Retorno"] = hist["data_proxima_acao"].apply(data_br)
    hist = hist[[
        "Data","tipo_contato","resultado","status_novo",
        "observacao","proxima_acao","Retorno"
    ]]
    hist.columns = [
        "Data","Tipo","Resultado","Status",
        "Observação","Próxima ação","Retorno"
    ]
    st.dataframe(hist, use_container_width=True, hide_index=True)


def campos_contato(prefixo, empresa, tentativa_num):
    st.markdown(f"**Tentativa {min(tentativa_num, 3)} de 3**")

    c1, c2 = st.columns(2)
    data_contato = c1.date_input(
        "Data do contato *",
        value=date.today(),
        max_value=date.today(),
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
        key=f"{prefixo}_obs"
    )

    proxima_acao = st.selectbox(
        "Próxima ação",
        ACOES_SUGERIDAS,
        key=f"{prefixo}_acao"
    )

    outra_acao = ""
    if proxima_acao == "OUTRO":
        outra_acao = st.text_input(
            "Descreva a próxima ação",
            key=f"{prefixo}_outra_acao"
        )

    st.caption(
        "Resultados de espera retornam automaticamente à fila após 20 novos contatos. "
        "Na 3ª tentativa sem retorno, o cliente é encerrado como SEM INTERESSE."
    )

    acao_final = outra_acao.strip() if proxima_acao == "OUTRO" else proxima_acao
    return data_contato, tipo, resultado, obs, acao_final

def campo_agendamento(prefixo):
    agendar = st.checkbox(
        "Definir uma data específica para retorno",
        key=f"{prefixo}_agendar"
    )

    data_agendamento = None
    if agendar:
        data_agendamento = st.date_input(
            "Data específica do retorno",
            value=date.today() + timedelta(days=1),
            min_value=date.today(),
            format="DD/MM/YYYY",
            key=f"{prefixo}_data_agendamento"
        )
        st.caption(
            "Esse cliente aparecerá como pendência a partir da data marcada "
            "e permanecerá no topo até ser atualizado."
        )

    return data_agendamento

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
# Na nuvem, a base oficial do GitHub é carregada antes de qualquer leitura.
if github_ativo():
    ok_pull, _msg_pull = carregar_base_github()

criar_banco()
importar_planilha_inicial()

# Se a branch de dados ainda não tinha base, publica a base inicial.
if github_ativo():
    _, _, _, _, _db_repo_path = github_config()
    try:
        if _github_get_file(_db_repo_path) is None:
            salvar_base_github()
    except Exception:
        pass

empresas = carregar_empresas()
contatos = carregar_contatos()

st.title("📈 Gestão Comercial")
st.caption("Prospecção, retornos, agendamentos e acompanhamento da carteira comercial.")

menu = st.sidebar.radio(
    "Menu",
    [
        "📊 Dashboard",
        "📞 Fila de contatos",
        "➕ Adicionar contatos em lote",
        "🏢 Empresas / Clientes",
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

# ---------------- FILA ÚNICA DE CONTATOS ----------------
elif menu == "📞 Fila de contatos":
    st.subheader("📞 Fila de contatos")
    st.caption("Prioridades, retornos e novos contatos em uma única tela de trabalho.")

    hoje = date.today()
    hoje_ts = pd.Timestamp(hoje)
    seq_atual = seq_global_atual()

    base = empresas.copy()
    base["ag_dt"] = pd.to_datetime(
        base["data_agendamento"], errors="coerce"
    ).dt.normalize()

    atrasados = base[
        (base["agendamento_pendente"] == 1) &
        base["ag_dt"].notna() &
        (base["ag_dt"] < hoje_ts) &
        (~base["status"].isin(STATUS_ENCERRADOS))
    ].copy()

    hoje_ag = base[
        (base["agendamento_pendente"] == 1) &
        base["ag_dt"].notna() &
        (base["ag_dt"] == hoje_ts) &
        (~base["status"].isin(STATUS_ENCERRADOS))
    ].copy()

    automaticos = base[
        base["retorno_apos_seq"].notna() &
        (base["retorno_apos_seq"] <= seq_atual) &
        (~base["status"].isin(STATUS_ENCERRADOS))
    ].copy()

    novos = base[
        (base["status"] == "SEM CONTATO") &
        (base["agendamento_pendente"].fillna(0) != 1)
    ].copy()

    # Cards de prioridade visíveis sempre no topo.
    c1,c2,c3,c4 = st.columns(4)
    with c1:
        card("🔴 Atrasados", len(atrasados), "permanecem até serem tratados")
    with c2:
        card("🟠 Para hoje", len(hoje_ag), "agendamentos do dia")
    with c3:
        card("🔄 Retornos", len(automaticos), "liberados após 20 contatos")
    with c4:
        card("🆕 Novos", len(novos), "ainda sem contato")

    # Ordem da fila: atrasados > hoje > retorno automático > novos.
    atrasados["_prioridade"] = 1
    hoje_ag["_prioridade"] = 2
    automaticos["_prioridade"] = 3
    novos["_prioridade"] = 4

    fila_df = pd.concat(
        [atrasados, hoje_ag, automaticos, novos],
        ignore_index=True
    ).drop_duplicates(subset=["id"], keep="first")

    if fila_df.empty:
        st.success("Não há contatos pendentes na fila.")
    else:
        fila_df = fila_df.sort_values(
            ["_prioridade","ag_dt","nome"],
            na_position="last"
        ).reset_index(drop=True)

        atual = fila_df.iloc[0]
        empresa_id = int(atual["id"])
        tent = tentativas_empresa(empresa_id) + 1

        # Mensagem da última gravação.
        msg = st.session_state.pop("flash_contato", None)
        if msg:
            st.success(msg)

        if atual["_prioridade"] == 1:
            st.error(
                f"🔴 PRIORIDADE — retorno atrasado desde "
                f"{atual['ag_dt'].strftime('%d/%m/%Y')}"
            )
        elif atual["_prioridade"] == 2:
            st.warning("🟠 PRIORIDADE — retorno agendado para hoje")
        elif atual["_prioridade"] == 3:
            st.info("🔄 RETORNO AUTOMÁTICO — cliente liberado após 20 novos contatos")
        else:
            st.info("🆕 NOVO CONTATO")

        c1,c2 = st.columns([4,1])
        with c1:
            st.markdown(f"### {atual['nome']}")
            st.write(f"**CPF/CNPJ:** {atual['documento'] or '-'}")
            telefones = [
                t for t in [
                    atual["telefone1"], atual["telefone2"], atual["telefone3"]
                ] if t and str(t).lower() != "nan"
            ]
            st.write(f"**Telefone(s):** {' | '.join(telefones) if telefones else '-'}")

            if atual["agendamento_pendente"] == 1 and pd.notna(atual["ag_dt"]):
                st.write(
                    f"**Retorno agendado:** {atual['ag_dt'].strftime('%d/%m/%Y')}"
                )
                if atual["proxima_acao"]:
                    st.write(f"**Ação prevista:** {atual['proxima_acao']}")

        with c2:
            card("Na fila", f"1/{len(fila_df)}", "próximo da prioridade")

        if atual["observacao_atual"]:
            st.info(f"**Última observação:** {atual['observacao_atual']}")

        # Histórico aparece para retorno; em cliente novo, só aparece se houver algo importado.
        hist_cliente = contatos[contatos["empresa_id"] == empresa_id]
        if not hist_cliente.empty:
            with st.expander("Ver histórico deste cliente"):
                historico_cliente(contatos, empresa_id)

        # As chaves incluem o ID do cliente. Ao avançar, o próximo cliente recebe campos novos/limpos.
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
            "Resultados de espera retornam automaticamente à fila após 20 novos contatos. "
            "Na 3ª tentativa sem retorno, o cliente é encerrado automaticamente como SEM INTERESSE."
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

        salvar = st.button(
            "Salvar e ir para o próximo",
            type="primary",
            use_container_width=True,
            key=f"{prefixo}_salvar"
        )

        if salvar:
            status_novo, tentativa_atual, retorno_apos = registrar_contato(
                empresa_id, data_contato, tipo, resultado, obs, acao, data_ag
            )

            if status_novo == "SEM INTERESSE" and resultado in RESULTADOS_AGUARDANDO:
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
                    "Voltará automaticamente após 20 novos contatos."
                )
            else:
                mensagem = f"{atual['nome']}: contato salvo com sucesso."

            st.session_state["flash_contato"] = mensagem

            # Limpa qualquer estado do cliente recém-salvo.
            for chave in list(st.session_state.keys()):
                if chave.startswith(prefixo):
                    del st.session_state[chave]

            # O rerun recalcula a fila. Como o cliente atual mudou de status/pendência,
            # o próximo registro assume imediatamente a tela.
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
elif menu == "🏢 Empresas / Clientes":
    st.subheader("🏢 Empresas / Clientes")
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

    mostrar = view[[
        "documento","nome","telefone1","telefone2","telefone3","status",
        "observacao_atual","data_primeiro_contato","proxima_acao","data_agendamento"
    ]].copy()
    mostrar["data_primeiro_contato"] = mostrar["data_primeiro_contato"].apply(data_br)
    mostrar["data_agendamento"] = mostrar["data_agendamento"].apply(data_br)
    mostrar.columns = [
        "CPF/CNPJ","Empresa / Cliente","Telefone 1","Telefone 2","Telefone 3",
        "Status","Última observação","1º contato","Próxima ação","Agendamento"
    ]
    st.caption(f"{len(mostrar)} registro(s)")
    st.dataframe(mostrar, use_container_width=True, hide_index=True, height=600)

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
            exibir = rel[[
                "data_contato","nome","documento","tipo_contato","resultado",
                "status_novo","observacao","proxima_acao","data_proxima_acao"
            ]].copy()
            exibir["data_contato"] = exibir["data_contato"].apply(data_br)
            exibir["data_proxima_acao"] = exibir["data_proxima_acao"].apply(data_br)
            exibir.columns = [
                "Data","Empresa / Cliente","CPF/CNPJ","Tipo","Resultado",
                "Status","Observação","Próxima ação","Data retorno"
            ]
            st.dataframe(exibir, use_container_width=True, hide_index=True)

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
    st.sidebar.success("☁️ Base GitHub conectada")
else:
    st.sidebar.warning("💻 Modo local")

if st.sidebar.button("🔄 Carregar base de dados", use_container_width=True):
    if github_ativo():
        ok, msg = carregar_base_github()
        if ok:
            st.sidebar.success(msg)
            st.rerun()
        else:
            st.sidebar.error(msg)
    else:
        st.sidebar.info(
            "No computador local, a base já está no arquivo database/comercial.db. "
            "No deploy, configure os Secrets do GitHub para habilitar a recuperação."
        )

st.sidebar.caption("Gestão Comercial • FINAL")

