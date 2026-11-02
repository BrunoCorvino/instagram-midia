"""Robô do Instagram do @brunocorvino.

Roda a cada 15 minutos (GitHub Actions) e faz duas coisas:
1. Quem comenta QUERO ou PROSPERAR num post recente recebe uma resposta no
   comentário e o PDF no Direct.
2. Quem responde no Direct recebe a próxima mensagem da conversa (roteiro.json).

Só mexe em conversas que o próprio robô começou: conversa pessoal fica de fora.
O log não mostra nomes nem mensagens, porque o repositório é público.
"""
import json
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://graph.instagram.com/v21.0"
TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
USER_ID = os.environ.get("IG_USER_ID", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

ROTEIRO = json.loads((Path(__file__).parent / "roteiro.json").read_text(encoding="utf-8"))
AGORA = datetime.now(timezone.utc)


def normalizar(texto):
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.upper().replace("!", " ").replace(".", " ").split())


def chamar(caminho, params=None, corpo=None):
    url = API + caminho
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": "Bearer " + TOKEN}
    dados = None
    if corpo is not None:
        dados = json.dumps(corpo).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=dados, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detalhe = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"Erro {e.code} em {caminho}: {detalhe}") from None


def data(texto):
    return datetime.strptime(texto, "%Y-%m-%dT%H:%M:%S%z")


def montar_mensagem(etapa):
    msg = {"text": etapa["texto"]}
    if etapa.get("botoes"):
        msg["quick_replies"] = [
            {"content_type": "text", "title": b, "payload": f"{etapa['id']}:{i}"}
            for i, b in enumerate(etapa["botoes"])
        ]
    return msg


def enviar(destino, etapa):
    if DRY_RUN:
        print(f"[teste] enviaria a etapa {etapa['id']}")
        return
    try:
        chamar(f"/{USER_ID}/messages", corpo={"recipient": destino, "message": montar_mensagem(etapa)})
    except RuntimeError:
        if not etapa.get("botoes"):
            raise
        # se o Instagram recusar os botões, manda só o texto
        chamar(f"/{USER_ID}/messages", corpo={"recipient": destino, "message": {"text": etapa["texto"]}})


def etapa_da_mensagem(texto):
    """Descobre qual etapa do roteiro é uma mensagem nossa, pela marca dela."""
    for etapa in ROTEIRO["etapas"] + [ROTEIRO["saida"]]:
        if any(m in (texto or "") for m in etapa["marcas"]):
            return etapa
    return None


def proxima_etapa(etapa_atual):
    ids = [e["id"] for e in ROTEIRO["etapas"]]
    i = ids.index(etapa_atual["id"])
    return ROTEIRO["etapas"][i + 1] if i + 1 < len(ids) else None


def resposta_negativa(texto, etapa):
    if not etapa.get("sair_se_nao"):
        return False
    t = normalizar(texto)
    return t in {"NAO", "NAO QUERO", "NAO OBRIGADO", "NAO OBRIGADA", "AGORA NAO"} or t.startswith("NAO ")


def resposta_curta_ou_botao(texto, etapa):
    """Botão ou resposta curta ("sim", "quero muito") o robô segue sozinho."""
    t = normalizar(texto)
    if t in {normalizar(b) for b in etapa.get("botoes", [])}:
        return True
    return bool(t) and len(t.split()) <= 4 and "?" not in (texto or "")


def conversa_do_usuario(igsid):
    r = chamar("/me/conversations", {"platform": "instagram", "user_id": igsid,
                                      "fields": "messages.limit(10){message,from,created_time}"})
    convs = r.get("data", [])
    return convs[0].get("messages", {}).get("data", []) if convs else []


def ja_recebeu_o_pdf(igsid):
    for m in conversa_do_usuario(igsid):
        if m["from"]["id"] != igsid and any(x in (m.get("message") or "") for x in ROTEIRO["etapas"][0]["marcas"]):
            return True
    return False


def cuidar_dos_comentarios(meu_nome):
    palavras = {normalizar(p) for p in ROTEIRO["palavras_chave"]}
    posts = chamar("/me/media", {"fields": "id,timestamp", "limit": 15}).get("data", [])
    novos = 0
    for post in posts:
        if AGORA - data(post["timestamp"]) > timedelta(days=7):
            continue  # o Instagram só deixa responder no Direct até 7 dias depois
        comentarios = chamar(f"/{post['id']}/comments", {
            "fields": "id,text,username,from,timestamp,replies{id,text}", "limit": 50}).get("data", [])
        nossos = {c["id"] for c in comentarios if c.get("username") == meu_nome}
        textos_nossos = {"Te mandei no Direct! 🙏", ROTEIRO["resposta_comentario"]}
        for c in comentarios:
            autor = c.get("from", {})
            if c.get("username") == meu_nome or autor.get("username") == meu_nome:
                continue
            texto = normalizar(c.get("text"))
            if not any(p in texto.split() or p == texto for p in palavras):
                continue
            respostas = c.get("replies", {}).get("data", [])
            if any(r["id"] in nossos or r.get("text") in textos_nossos for r in respostas):
                continue  # já respondido
            novos += 1
            if DRY_RUN:
                print("[teste] responderia um comentário e mandaria o PDF")
                continue
            try:
                if not ja_recebeu_o_pdf(autor["id"]):
                    enviar({"comment_id": c["id"]}, ROTEIRO["etapas"][0])
                chamar(f"/{c['id']}/replies", corpo={"message": ROTEIRO["resposta_comentario"]})
            except RuntimeError as e:
                print("Falha num comentário:", str(e)[:120])
            time.sleep(2)
    return novos


def cuidar_do_direct(meu_id):
    r = chamar("/me/conversations", {"platform": "instagram", "limit": 25,
                                      "fields": "updated_time,participants,messages.limit(3){message,from,created_time}"})
    enviados = 0
    for conv in r.get("data", []):
        if AGORA - data(conv["updated_time"]) > timedelta(hours=23):
            continue  # fora da janela de 24h do Instagram
        msgs = conv.get("messages", {}).get("data", [])  # mais nova primeiro
        if not msgs or msgs[0]["from"]["id"] == meu_id:
            continue  # a última mensagem é nossa: esperando a pessoa
        ultima_nossa = next((m for m in msgs if m["from"]["id"] == meu_id), None)
        if not ultima_nossa:
            continue
        etapa = etapa_da_mensagem(ultima_nossa.get("message"))
        if not etapa or etapa["id"] == ROTEIRO["saida"]["id"]:
            continue  # conversa que não é do robô, ou já encerrada
        resposta = msgs[0].get("message") or ""
        if not resposta_curta_ou_botao(resposta, etapa):
            continue  # resposta solta: quem responde é o Claude, na checagem de hora em hora
        if resposta_negativa(resposta, etapa):
            seguinte = ROTEIRO["saida"]
        else:
            seguinte = proxima_etapa(etapa)
        if not seguinte:
            continue  # roteiro terminou: daqui para frente é o BC
        pessoa = msgs[0]["from"]["id"]
        try:
            enviar({"id": pessoa}, seguinte)
        except RuntimeError as e:
            print("Falha numa conversa:", str(e)[:120])
            continue
        enviados += 1
        time.sleep(2)
    return enviados


def listar_pendentes(meu_id):
    """Para o Claude: conversas do robô com resposta solta esperando resposta."""
    r = chamar("/me/conversations", {"platform": "instagram", "limit": 25,
                                      "fields": "updated_time,messages.limit(8){message,from,created_time}"})
    pendentes = []
    for conv in r.get("data", []):
        if AGORA - data(conv["updated_time"]) > timedelta(hours=23):
            continue
        msgs = conv.get("messages", {}).get("data", [])
        if not msgs or msgs[0]["from"]["id"] == meu_id:
            continue
        ultima_nossa = next((m for m in msgs if m["from"]["id"] == meu_id), None)
        etapa = etapa_da_mensagem(ultima_nossa.get("message")) if ultima_nossa else None
        if not etapa:
            continue
        if etapa["id"] != ROTEIRO["saida"]["id"] and resposta_curta_ou_botao(msgs[0].get("message"), etapa) \
                and proxima_etapa(etapa) is not None:
            continue  # o robô cuida
        pendentes.append({
            "pessoa_id": msgs[0]["from"]["id"], "username": msgs[0]["from"].get("username"),
            "ultima_etapa_do_robo": etapa["id"],
            "proxima_etapa": (proxima_etapa(etapa) or {}).get("id"),
            "conversa": [{"de": "eu" if m["from"]["id"] == meu_id else "pessoa", "texto": m.get("message"),
                          "quando": m["created_time"]} for m in reversed(msgs)],
        })
    print(json.dumps(pendentes, ensure_ascii=False, indent=1))


def responder(pessoa_id, texto=None, etapa_id=None):
    """Para o Claude: manda um texto livre e/ou uma etapa do roteiro."""
    if texto:
        chamar(f"/{USER_ID}/messages", corpo={"recipient": {"id": pessoa_id}, "message": {"text": texto}})
    if etapa_id:
        etapa = next(e for e in ROTEIRO["etapas"] + [ROTEIRO["saida"]] if e["id"] == etapa_id)
        enviar({"id": pessoa_id}, etapa)
    print("enviado")


def main():
    if not TOKEN or not USER_ID:
        sys.exit("Faltam IG_ACCESS_TOKEN e IG_USER_ID.")
    eu = chamar("/me", {"fields": "user_id,username"})
    if len(sys.argv) > 1 and sys.argv[1] == "--pendentes":
        return listar_pendentes(eu["user_id"])
    if len(sys.argv) > 1 and sys.argv[1] == "--responder":
        # uso: --responder PESSOA_ID "texto livre" [ETAPA_ID]
        return responder(sys.argv[2], sys.argv[3] or None, sys.argv[4] if len(sys.argv) > 4 else None)
    c = cuidar_dos_comentarios(eu["username"])
    d = cuidar_do_direct(eu["user_id"])
    print(f"Comentários respondidos: {c}. Mensagens do Direct enviadas: {d}.")


if __name__ == "__main__":
    main()
