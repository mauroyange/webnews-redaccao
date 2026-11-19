"""Ajudante da redacção WebNews.ao: WordPress (REST) e imagens.

Só define funções: importar este módulo não faz nenhum pedido nem cria nada.
Credenciais lidas das variáveis de ambiente WN_USER e WN_APP_PASS (nunca escritas em ficheiros).
"""
import hashlib
import io
import os
import re
import time
import urllib.parse

import requests

PORTAIS = ("agro", "petro", "money", "tech", "life", "sport")
UA = "WebNewsRedaccao/1.0 (https://webnews.ao; info@webnews.ao)"
ESTADOS_PROIBIDOS = {"publish", "future", "private"}


class WP:
    """Cliente REST de um portal. Só cria matérias com status 'pending'."""

    def __init__(self, portal):
        if portal not in PORTAIS:
            raise ValueError(f"portal desconhecido: {portal}")
        self.portal = portal
        self.host = f"https://{portal}.webnews.ao"
        self.base = f"{self.host}/wp-json/wp/v2"
        self.s = requests.Session()
        self.s.auth = (os.environ["WN_USER"], os.environ["WN_APP_PASS"])
        self.s.headers["User-Agent"] = UA

    # ---- base -------------------------------------------------------------
    def _r(self, method, path, **kw):
        r = self.s.request(method, self.base + path, timeout=90, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def verificar_acesso(self):
        """Devolve (slug, papéis). Falha se a rede ou a autenticação não funcionarem."""
        me = self._r("GET", "/users/me", params={"context": "edit"})
        return me["slug"], me["roles"]

    # ---- leitura ----------------------------------------------------------
    def recentes(self, dias=14):
        """Títulos de todas as matérias (qualquer estado) dos últimos `dias`, para não repetir temas."""
        depois = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - dias * 86400))
        return self._r("GET", "/posts", params={
            "status": "publish,pending,draft,future,private", "after": depois, "per_page": 100,
            "context": "edit", "_fields": "id,status,date,title,link"})

    def procurar(self, termo):
        """Matérias (qualquer estado) que contêm o termo: usar antes de criar, para não duplicar."""
        return self._r("GET", "/posts", params={
            "status": "publish,pending,draft,future,private", "search": termo, "per_page": 20,
            "context": "edit", "_fields": "id,status,title"})

    def categorias(self):
        return {c["id"]: c["name"] for c in self._r("GET", "/categories", params={"per_page": 100})}

    def etiquetas(self, nomes):
        """IDs das etiquetas: reutiliza a existente (nome exacto, sem diferenciar maiúsculas) ou cria."""
        ids = []
        for nome in nomes:
            achadas = self._r("GET", "/tags", params={"search": nome, "per_page": 100})
            igual = [t for t in achadas if t["name"].strip().lower() == nome.strip().lower()]
            if igual:
                ids.append(igual[0]["id"])
            else:
                ids.append(self._r("POST", "/tags", json={"name": nome})["id"])
        return ids

    # ---- escrita ----------------------------------------------------------
    def carregar_imagem(self, caminho, alt, legenda, titulo):
        # Envio em multipart/form-data: o servidor (OpenLiteSpeed/CyberPanel) recusa com 403
        # o envio do ficheiro em bruto com Content-Disposition.
        nome = os.path.basename(caminho)
        with open(caminho, "rb") as f:
            m = self._r("POST", "/media", files={"file": (nome, f, "image/jpeg")})
        self._r("POST", f"/media/{m['id']}", json={"alt_text": alt, "caption": legenda, "title": titulo})
        return m["id"]

    def associar_imagem(self, media_id, post_id):
        self._r("POST", f"/media/{media_id}", json={"post": post_id})

    def criar_pendente(self, titulo, slug, entrada, conteudo, categorias, etiquetas, imagem_id=None):
        dados = {"status": "pending", "title": titulo, "slug": slug, "excerpt": entrada,
                 "content": conteudo, "categories": categorias, "tags": etiquetas,
                 "comment_status": "closed"}
        if imagem_id:
            dados["featured_media"] = imagem_id
        p = self._r("POST", "/posts", json=dados)
        try:
            self.verificar(p["id"])
        except Exception as e:
            raise RuntimeError(f"post {p['id']} criado, mas a verificação falhou: {e}") from e
        return p["id"]

    def verificar(self, post_id):
        """Confirma que a matéria está Pendente e não é pública. Repõe 'pending' se for preciso."""
        p = self._r("GET", f"/posts/{post_id}", params={"context": "edit"})
        if p["status"] in ESTADOS_PROIBIDOS:
            self._r("POST", f"/posts/{post_id}", json={"status": "pending"})
            raise RuntimeError(f"ALERTA: post {post_id} estava '{p['status']}' e foi reposto como pending")
        if p["status"] != "pending":
            raise RuntimeError(f"post {post_id} ficou com status '{p['status']}'")
        anon = requests.get(f"{self.base}/posts/{post_id}", headers={"User-Agent": UA}, timeout=60)
        if anon.status_code not in (401, 403, 404, 503):
            raise RuntimeError(f"post {post_id} responde {anon.status_code} sem autenticação")
        return {"status": p["status"], "featured_media": p["featured_media"],
                "categories": p["categories"], "tags": p["tags"], "anonimo": anon.status_code}

    def links(self, post_id):
        return {"editar": f"{self.host}/wp-admin/post.php?post={post_id}&action=edit",
                "pre_visualizar": f"{self.host}/?p={post_id}&preview=true"}


# ---- imagens ----------------------------------------------------------------
def _get(url, tentativas=4, espera=20):
    """GET com espera crescente (20, 40, 80 s) quando o servidor responde 429."""
    for i in range(tentativas):
        r = requests.get(url, headers={"User-Agent": UA}, timeout=90)
        if r.status_code == 429 and i < tentativas - 1:
            pedido = r.headers.get("Retry-After", "")
            time.sleep(max(int(pedido) if pedido.isdigit() else 0, espera * (2 ** i)))
            continue
        r.raise_for_status()
        return r
    raise RuntimeError(f"falhou: {url}")


def wikimedia_original(ficheiro):
    """Bytes da imagem no Wikimedia Commons (nome sem 'File:').
    Pede primeiro a miniatura normalizada de 1920 px (o Commons recusa muitas vezes o original
    com 429); se o original for mais estreito, pede o original."""
    nome = ficheiro.replace(" ", "_")
    h = hashlib.md5(nome.encode("utf-8")).hexdigest()
    q = urllib.parse.quote(nome)
    pasta = f"https://upload.wikimedia.org/wikipedia/commons"
    try:
        return _get(f"{pasta}/thumb/{h[0]}/{h[:2]}/{q}/1920px-{q}").content
    except requests.HTTPError as e:
        if e.response is None or e.response.status_code == 429:
            raise
    return _get(f"{pasta}/{h[0]}/{h[:2]}/{q}").content


def wikimedia_ficha(ficheiro):
    """Autor e licença lidos da página do ficheiro (texto wiki). Confirmar sempre o resultado."""
    nome = ficheiro.replace(" ", "_")
    url = ("https://commons.wikimedia.org/w/index.php?title=File:"
           + urllib.parse.quote(nome) + "&action=raw")
    txt = _get(url).text
    autor = re.search(r"\|\s*[Aa]uthor\s*=\s*(.+)", txt)
    licencas = re.findall(r"\{\{\s*((?:[Cc]c-[^}|]+)|(?:PD-[^}|]+)|(?:GFDL[^}|]*)|(?:[Ss]elf\|[^}]+))", txt)
    return {"autor": autor.group(1).strip() if autor else None, "licencas": licencas,
            "pagina": "https://commons.wikimedia.org/wiki/File:" + urllib.parse.quote(nome)}


def pexels_original(foto_id):
    return _get(f"https://images.pexels.com/photos/{foto_id}/pexels-photo-{foto_id}.jpeg?w=2000").content


def nappy_procurar(termo, maximo=12):
    """Fotos do Nappy (licença CC0; pessoas negras e pardas) para um termo em inglês.
    Devolve [{id, titulo, pagina}]. Só lê nappy.co; não descarrega nada."""
    url = "https://nappy.co/search/" + urllib.parse.quote(termo.strip())
    t = _get(url).text
    vistos, fotos = set(), []
    for titulo, fid in re.findall(r'<img[^>]+alt="([^"]*)"[^>]+images\.nappy\.co/photo/([A-Za-z0-9_-]+)\.', t):
        if fid not in vistos:
            vistos.add(fid)
            fotos.append({"id": fid, "titulo": titulo, "pagina": f"https://nappy.co/photo/{fid}"})
    return fotos[:maximo]


def nappy_ficha(foto_id):
    """Título, autor (@utilizador) e largura máxima disponível de uma foto do Nappy."""
    t = _get(f"https://nappy.co/photo/{foto_id}").text
    m = re.search(r"<title>(.*?)\s*\|", t, re.S)
    titulo = (m.group(1).strip() if m else "").replace("&amp;", "&")
    autor = re.search(r"@([A-Za-z0-9_.-]+)\s*$", titulo)
    larguras = [int(w) for w in re.findall(re.escape(foto_id) + r"\.jpg\?width=(\d+)", t)]
    return {"titulo": re.sub(r"\s*@[A-Za-z0-9_.-]+\s*$", "", titulo), "autor": autor.group(1) if autor else None,
            "largura_max": max(larguras) if larguras else None, "licenca": "CC0 (Nappy)",
            "pagina": f"https://nappy.co/photo/{foto_id}"}


def nappy_original(foto_id, largura=1920):
    """Bytes da foto em images.nappy.co. Esse domínio tem de estar autorizado nas definições do Claude;
    se não estiver, o pedido falha (erro de proxy) e passa-se à fonte seguinte."""
    return _get(f"https://images.nappy.co/photo/{foto_id}.jpg?width={largura}").content


def enquadrar(conteudo, saida, foco=(0.5, 0.4), largura=1200, altura=675):
    """Corta a maior caixa 16:9 que contém o foco (fracções x, y da imagem) e põe o foco
    perto do terço superior; redimensiona e grava JPEG 85. Recusa imagens com menos de 1000 px."""
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(conteudo))).convert("RGB")
    w, h = im.size
    if w < 1000:
        raise ValueError(f"imagem pequena demais ({w}px)")
    alvo = largura / altura
    if w / h > alvo:
        cw, ch = int(h * alvo), h
    else:
        cw, ch = w, int(w / alvo)
    fx, fy = foco[0] * w, foco[1] * h
    x0 = min(max(int(fx - cw / 2), 0), w - cw)
    y0 = min(max(int(fy - ch / 3), 0), h - ch)
    im.crop((x0, y0, x0 + cw, y0 + ch)).resize((largura, altura), Image.LANCZOS).save(
        saida, "JPEG", quality=85, optimize=True, progressive=True)
    return saida


def folha_previa(caminhos, saida):
    """Junta as imagens enquadradas numa folha para ver com Read antes de enviar."""
    from PIL import Image
    ims = [Image.open(c) for c in caminhos]
    folha = Image.new("RGB", (600 * len(ims), 338), "white")
    for i, im in enumerate(ims):
        folha.paste(im.resize((600, 338)), (600 * i, 0))
    folha.save(saida, "JPEG", quality=80)
    return saida
