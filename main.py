#!/usr/bin/env python3
"""
Market Oracle — Railway Cloud Worker
Autor: generado por Claude para Juan Rafael

FLUJO DIARIO:
  15:25 España → Análisis pre-market + guarda recomendaciones en Supabase (9:25 AM ET)
  15:30 España → Mercado abre → entra en trades recomendados (paper)
  15:30-22:00  → Cada 5 min revisa precios → detecta target/stop
  22:00 España → Cierra posiciones abiertas → guarda P&L del día
  22:05 España → Resumen diario en consola
"""

import os, time, json, requests, schedule
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

# ─── CONFIGURACIÓN (variables de entorno en Railway) ──────────────
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
POLYGON_KEY   = os.environ.get("POLYGON_KEY",   "1AVXtXf754mtMFxgV6mAdGuwCEjW9FU9")
FMP_KEY       = os.environ.get("FMP_KEY",        "PRvdQPOqfEFT8IE28P5hTRQzxmrc1a3C")
SUPABASE_URL  = os.environ.get("SUPABASE_URL",   "")   # https://xxx.supabase.co
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY",   "")   # anon public key

TZ_SPAIN = ZoneInfo("Europe/Madrid")
TZ_ET    = ZoneInfo("America/New_York")

# ─── TWELVE DATA — PRECIOS EN TIEMPO REAL ─────────────────────────
TWELVE_KEY = os.environ.get("TWELVE_KEY", "dff5698aa9f54d74978ba01360d62b74")

def get_realtime_prices(tickers):
    """Obtiene precios en tiempo real vía Twelve Data (incluyendo pre-market)"""
    if not tickers:
        return {}
    symbols = ",".join(tickers[:8])
    results = {}
    try:
        # Quote completo con pre-market
        url = f"https://api.twelvedata.com/quote?symbol={symbols}&apikey={TWELVE_KEY}"
        req = urllib.request.Request(url, headers={"User-Agent": "market-oracle"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        
        # Si es un solo ticker, Twelve Data devuelve el objeto directamente
        if len(tickers) == 1:
            data = {tickers[0]: data}
        
        for sym, q in data.items():
            if isinstance(q, dict) and "close" in q:
                price = float(q.get("close", 0))
                prev_close = float(q.get("previous_close", 0))
                open_price = float(q.get("open", 0)) if q.get("open") else None
                pre_market = float(q.get("pre_market", 0)) if q.get("pre_market") else None
                
                # Precio más actual disponible: pre-market > open > close
                current = pre_market or open_price or price
                gap_pct = ((current - prev_close) / prev_close * 100) if prev_close else 0
                
                results[sym] = {
                    "current": round(current, 2),
                    "prev_close": round(prev_close, 2),
                    "open": round(open_price, 2) if open_price else None,
                    "pre_market": round(pre_market, 2) if pre_market else None,
                    "gap_pct": round(gap_pct, 2),
                    "source": "pre_market" if pre_market else ("open" if open_price else "close")
                }
                log(f"  {sym}: ${current:.2f} ({'+' if gap_pct>=0 else ''}{gap_pct:.2f}% vs cierre) [{results[sym]['source']}]")
        
        return results
    except Exception as e:
        log(f"Twelve Data error: {e}", "WARN")
        return {}



# ─── LOGGING ──────────────────────────────────────────────────────
def log(msg, level="INFO"):
    now = datetime.now(TZ_SPAIN).strftime("%Y-%m-%d %H:%M:%S")
    prefix = {"INFO":"ℹ️","OK":"✅","WARN":"⚠️","ERR":"❌","TRADE":"📊","MONEY":"💰"}
    print(f"[{now}] {prefix.get(level,'·')} {msg}", flush=True)

# ─── SUPABASE ─────────────────────────────────────────────────────
def sb(method, table, data=None, params=None):
    """Llamada genérica a Supabase REST API"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        log("Supabase no configurado — los datos no se guardarán", "WARN")
        return None
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    try:
        r = getattr(requests, method)(url, headers=headers, json=data, params=params, timeout=15)
        return r.json() if r.ok and r.text else None
    except Exception as e:
        log(f"Supabase error: {e}", "ERR")
        return None

def sb_insert(table, data):
    return sb("post", table, data)

def sb_update(table, data, match_col, match_val):
    return sb("patch", table, data, params={match_col: f"eq.{match_val}"})

def sb_select(table, params=None):
    return sb("get", table, params=params)

# ─── POLYGON ──────────────────────────────────────────────────────
def poly_get(path):
    sep = "&" if "?" in path else "?"
    try:
        r = requests.get(f"https://api.polygon.io{path}{sep}apiKey={POLYGON_KEY}", timeout=12)
        return r.json() if r.ok else None
    except:
        return None

def get_poly(tickers):
    results = {}
    for sym in tickers[:8]:
        try:
            prev = poly_get(f"/v2/aggs/ticker/{sym}/prev")
            to_d = (date.today() - timedelta(days=1)).isoformat()
            fr_d = (date.today() - timedelta(days=31)).isoformat()
            aggs = poly_get(f"/v2/aggs/ticker/{sym}/range/1/day/{fr_d}/{to_d}?adjusted=true&sort=desc&limit=30")
            pb = (prev or {}).get("results", [{}])[0]
            bars = (aggs or {}).get("results", [])
            av = int(sum(b["v"] for b in bars) / len(bars)) if bars else None
            results[sym] = {
                "prevClose": pb.get("c"),
                "avgVol": av,
                "volRatio": f"{pb['v']/av:.1f}x" if av and pb.get("v") else None,
                "atr14": f"{sum(b['h']-b['l'] for b in bars[:14])/14/pb['c']*100:.2f}%" if len(bars)>=14 and pb.get("c") else None,
                "h52": max((b["h"] for b in bars), default=None),
                "l52": min((b["l"] for b in bars), default=None),
            }
        except:
            results[sym] = None
    return results

# ─── FMP ──────────────────────────────────────────────────────────
def fmp_get(path):
    sep = "&" if "?" in path else "?"
    try:
        r = requests.get(f"https://financialmodelingprep.com/api{path}{sep}apikey={FMP_KEY}", timeout=12)
        return r.json() if r.ok else None
    except:
        return None

def get_fmp(tickers):
    today = date.today().isoformat()
    res = {}
    earnings = fmp_get(f"/v3/earning_calendar?from={today}&to={today}") or []
    upgrades = (fmp_get("/v4/upgrades-downgrades?page=0") or [])[:30]
    em = {e["symbol"]: e for e in earnings}
    um = {}
    for u in upgrades:
        um.setdefault(u["symbol"], []).append(u)
    for sym in tickers[:8]:
        try:
            q = (fmp_get(f"/v3/quote/{sym}") or [{}])[0]
            ins = (fmp_get(f"/v4/insider-trading?symbol={sym}&page=0") or [])[:3]
            e, u = em.get(sym), um.get(sym, [{}])
            res[sym] = {
                "price": q.get("price"),
                "changePct": f"{q.get('changesPercentage',0):.2f}%",
                "cap": f"{q.get('marketCap',0)/1e9:.1f}B" if q.get("marketCap") else None,
                "hasEarnings": bool(e),
                "earnTime": e.get("time") if e else None,
                "epsEst": e.get("epsEstimated") if e else None,
                "action": u[0].get("action") if u[0] else None,
                "firm": u[0].get("gradingCompany") if u[0] else None,
                "fromGrade": u[0].get("previousGrade") if u[0] else None,
                "toGrade": u[0].get("newGrade") if u[0] else None,
                "insideBuy": any("buy" in (i.get("transactionType","")).lower() for i in ins),
            }
        except:
            res[sym] = None
    res["_today"] = ", ".join(e["symbol"] for e in earnings) or "ninguno"
    return res

def get_current_price(sym):
    """Precio actual via Twelve Data (tiempo real) con fallback a FMP"""
    try:
        url = f"https://api.twelvedata.com/price?symbol={sym}&apikey={TWELVE_KEY}"
        req = urllib.request.Request(url, headers={"User-Agent": "market-oracle"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        price = float(data.get("price", 0))
        if price:
            return price
    except:
        pass
    # Fallback a FMP
    try:
        q = (fmp_get(f"/v3/quote/{sym}") or [{}])[0]
        return float(q.get("price", 0)) or None
    except:
        return None

# ─── CLAUDE ───────────────────────────────────────────────────────
def parse_json(text):
    text = text.replace("```json","").replace("```","").strip()
    s, e = text.find("{"), text.rfind("}")
    if s == -1: raise ValueError(f"Sin JSON en respuesta")
    return json.loads(text[s:e+1])

def call_claude(system, user, max_tokens=8000):
    if not ANTHROPIC_KEY:
        raise ValueError("Falta ANTHROPIC_KEY como variable de entorno en Railway")
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"Content-Type":"application/json","x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01"},
        json={
            "model": "claude-sonnet-4-5",
            "max_tokens": max_tokens,
            "tools": [{"type":"web_search_20250305","name":"web_search"}],
            "system": system,
            "messages": [{"role":"user","content":user}]
        },
        timeout=120
    )
    if not r.ok:
        raise Exception(f"HTTP {r.status_code}: {r.json().get('error',{}).get('message','')}")
    data = r.json()
    text = "".join(b["text"] for b in data.get("content",[]) if b["type"]=="text")
    return parse_json(text)

# ─── PROMPTS ──────────────────────────────────────────────────────
SCREENER_PROMPT = """Eres un screener bursatil. Busca en Yahoo Finance las acciones mas activas hoy.
Busca: yahoo finance most active gainers losers pre-market movers today earnings today.
Selecciona las 8 mejores con gap mayor 1% o earnings hoy o volumen inusual. Market cap mayor 300M.
RESPONDE SOLO JSON empezando con { sin texto antes ni despues sin comillas en strings:
{"at":"HH:MM ET","es":"+X%","nq":"+X%","vix":"XX","bias":"bullish|bearish|neutral","c":[{"t":"SYM","n":"Name","ch":"+X%","w":"razon","cat":"gainer|loser|active|earnings"}],"earn":["SYM"],"news":"titular macro"}"""

def build_prompt(poly, fmp):
    pc = "\n".join(f"{s}:close=${d['prevClose']} avgVol={d.get('avgVol','?')} ratio={d.get('volRatio','?')} ATR={d.get('atr14','?')}"
                   for s,d in poly.items() if d) or "n/d"
    fc_lines = []
    for s,d in fmp.items():
        if not d or s=="_today": continue
        l=[f"{s}:${d.get('price','?')} {d.get('changePct','?')} cap={d.get('cap','?')}"]
        if d.get("hasEarnings"): l.append(f"  EARNINGS HOY {d.get('earnTime','?')} EPS=${d.get('epsEst','?')}")
        if d.get("action"): l.append(f"  ANALYST:{d.get('firm','?')} {d.get('action','?')} {d.get('fromGrade','?')}->{d.get('toGrade','?')}")
        if d.get("insideBuy"): l.append("  INSIDER_BUY")
        fc_lines.append("\n".join(l))
    fc_joined = "\n".join(fc_lines) if fc_lines else "n/d"
    earnings_today = fmp.get('_today','ninguno')
    return f"""Analista cuantitativo day trading. Selecciona 3 trades para ganar mas 2% hoy.

POLYGON: {pc}
FMP: {fc_joined}
Earnings hoy: {earnings_today}

BLOQUES OBLIGATORIOS POR ACCION:
1-CATALIZADOR: nuevo hoy o ya descontado?
2-GAP TRAMPA: gap mayor 5%=quemado gapTrap=true esperar retroceso
3-CONTRA-NOTICIAS: busca noticias negativas cada una -10pts probabilidad
4-RIESGO REGULATORIO: proyectos federales subsidios litigios -8pts
5-TECNICOS: RSI14 EMA9/20 soporte resistencia patron volumen
6-OPCIONES/SHORT: PCR IV unusual short_float days_to_cover
7-ENTRADA PRECISA: gap<3%=open gap3-5%=wait15 gap>5%=wait_retrace con precio exacto

PROBABILIDAD: cat(25)+tech(20)+vol(15)+sec(10)+opt(10)+mac(8)+atr(7)+sho(5)
DESCUENTOS AUTOMATICOS: gap quemado=-15pts contra_noticia=-10pts riesgo_reg=-8pts

REGLA: {{ al inicio }} al final. Sin comillas en strings. Max 80 chars texto.

{{"date":"YYYY-MM-DD","at":"HH:MM ET","env":"ideal|good|difficult|avoid","vix":0.0,"spx":"s","summary":"s","score":0,"trades":[{{"t":"SYM","name":"s","dir":"long|short","prob":0,"label":"MUY ALTA|ALTA|MODERADA|BAJA","strat":"s","gapPct":"s","gapTrap":false,"gapWarn":"s","regulatoryRisk":false,"regulatoryDetail":"s","counterNews":["s"],"entryStrategy":"open|wait15|wait_retrace","entryPrice":0.0,"entryCondition":"s","sc":{{"cat":0,"tech":0,"vol":0,"sec":0,"opt":0,"mac":0,"atr":0,"sho":0}},"t1":0.0,"t2":0.0,"stop":0.0,"rr":"s","gain":"s","loss":"s","win":"s","rsi":0,"macd":"bull|bear","ema9":0.0,"ema20":0.0,"sup":0.0,"res":0.0,"pat":"s","pcr":0.0,"iv":"s","sf":"s","dtc":0.0,"cats":["s"],"risks":["s"],"note":"s"}}],"news":[{{"h":"s","src":"s","tk":"SYM","cat":"s","sent":"bull|bear|neu","imp":"high|med|low"}}],"rej":[{{"t":"s","r":"s"}}],"disc":"s"}}

3 trades. 5 noticias. JSON compacto."""

# ─── ESTADO EN MEMORIA ────────────────────────────────────────────
# Guarda trades activos durante la sesión
active_trades = {}  # {sym: {entry, target, stop, dir, recommendation_id}}

# ─── TAREA 1: ANÁLISIS 15:25 ──────────────────────────────────────
def task_analysis():
    """Corre el análisis completo y guarda recomendaciones en Supabase"""
    log("═" * 55)
    log("INICIANDO ANÁLISIS PRE-MARKET", "TRADE")
    log("═" * 55)

    try:
        today = datetime.now(TZ_SPAIN).strftime("%A %d de %B %Y")
        # Fase 1: Screener
        log("Fase 1/4: Yahoo Finance screener...")
        sc = call_claude(SCREENER_PROMPT, f"Hoy {today}. Busca movers. Solo JSON con {{", 3000)
        tickers = [c["t"] for c in sc.get("c",[]) if c.get("t")]
        log(f"→ {len(tickers)} candidatas: {', '.join(tickers)}", "OK")

        # Fase 2: Polygon
        log("Fase 2/4: Polygon.io datos históricos...")
        poly = get_poly(tickers) if tickers else {}
        log(f"→ {sum(1 for v in poly.values() if v)} tickers con datos", "OK")

        # Fase 3: FMP
        log("Fase 3/4: FMP earnings/upgrades/insider...")
        fmp = get_fmp(tickers) if tickers else {}
        log(f"→ Earnings hoy: {fmp.get('_today','ninguno')}", "OK")

        # Fase 3.5: Twelve Data precios en tiempo real
        log("Fase 3.5/4: Twelve Data precios en tiempo real...")
        td_prices = get_realtime_prices(tickers) if tickers else {}
        log(f"  → {len(td_prices)} precios en tiempo real", "OK")

        # Fase 4: Claude análisis
        log("Fase 4/4: Análisis IA (60-90s)...")
        list_str = "; ".join(f"{c['t']} {c.get('ch','')} {c.get('w','')}" for c in sc.get("c",[]))
        td_str = "\n".join(
            f"{s}: PRECIO_ACTUAL=${d['current']} ({'+' if d['gap_pct']>=0 else ''}{d['gap_pct']}% vs cierre_ayer) [fuente:{d['source']}] prev_close=${d['prev_close']}"
            + (f" open=${d['open']}" if d.get('open') else "")
            + (f" PRE-MARKET=${d['pre_market']}" if d.get('pre_market') else "")
            for s, d in td_prices.items()
        ) or "No disponible"
        analysis = call_claude(
            build_prompt(poly, fmp),
            f"Hoy {today}. Candidatas: {list_str}. ES={sc.get('es','?')} NQ={sc.get('nq','?')} VIX={sc.get('vix','?')}. Earnings:{fmp.get('_today','ninguno')}. Solo JSON con {{",
            10000
        )
        trades = analysis.get("trades", [])
        log(f"→ {len(trades)} trades recomendados | VIX: {analysis.get('vix','?')} | Entorno: {analysis.get('env','?').upper()}", "OK")

        # Guardar análisis diario en Supabase
        today_iso = date.today().isoformat()
        sb_insert("daily_analysis", {
            "date": today_iso,
            "analysis_time": datetime.now(TZ_SPAIN).isoformat(),
            "vix": analysis.get("vix"),
            "spx_futures": analysis.get("spx"),
            "environment": analysis.get("env"),
            "score": analysis.get("score"),
            "market_bias": sc.get("bias"),
            "summary": analysis.get("summary","")[:500],
            "screened_count": len(tickers),
            "trades_count": len(trades)
        })

        # Guardar cada trade recomendado
        global active_trades
        active_trades = {}
        for t in trades:
            sym = t.get("t","")
            if not sym: continue
            rec_data = {
                "date": today_iso,
                "ticker": sym,
                "company_name": t.get("name",""),
                "direction": t.get("dir","long"),
                "strategy": t.get("strat",""),
                "probability": t.get("prob",0),
                "probability_label": t.get("label",""),
                "gap_pct": t.get("gapPct",""),
                "gap_trap": t.get("gapTrap",False),
                "gap_warn": t.get("gapWarn",""),
                "regulatory_risk": t.get("regulatoryRisk",False),
                "regulatory_detail": t.get("regulatoryDetail",""),
                "counter_news": json.dumps(t.get("counterNews",[])),
                "entry_strategy": t.get("entryStrategy","open"),
                "rec_entry_price": t.get("entryPrice",0),
                "rec_target1": t.get("t1",0),
                "rec_target2": t.get("t2",0),
                "rec_stop": t.get("stop",0),
                "risk_reward": t.get("rr",""),
                "rsi": t.get("rsi",0),
                "pattern": t.get("pat",""),
                "catalysts": json.dumps(t.get("cats",[])),
                "risks": json.dumps(t.get("risks",[])),
                "entry_condition": t.get("entryCondition",""),
                "note": t.get("note",""),
                # Campos de resultado (se rellenan más tarde)
                "actual_entry_price": None,
                "actual_exit_price": None,
                "entry_time": None,
                "exit_time": None,
                "exit_reason": None,   # TARGET1 | TARGET2 | STOP | EOD | MANUAL
                "pnl_pct": None,
                "pnl_usd": None,       # basado en $10,000 por trade
                "result": None,        # WIN | LOSS | SCRATCH
                "status": "PENDING"    # PENDING | ACTIVE | CLOSED
            }
            result = sb_insert("trades", rec_data)
            rec_id = result[0]["id"] if result else None

            # Preparar para seguimiento durante sesión
            active_trades[sym] = {
                "id": rec_id,
                "dir": t.get("dir","long"),
                "entry": t.get("entryPrice",0),
                "target1": t.get("t1",0),
                "target2": t.get("t2",0),
                "stop": t.get("stop",0),
                "strategy": t.get("entryStrategy","open"),
                "status": "PENDING",
                "actual_entry": None
            }

            # Mostrar en consola
            gap_icon = " ⚠️ GAP TRAMPA" if t.get("gapTrap") else ""
            reg_icon = " 🏛" if t.get("regulatoryRisk") else ""
            log(f"{'▲' if t.get('dir')=='long' else '▼'} {sym} ({t.get('prob',0)}% {t.get('label','')}) | Entrada: ${t.get('entryPrice',0)} | T1: ${t.get('t1',0)} | Stop: ${t.get('stop',0)}{gap_icon}{reg_icon}", "TRADE")
            if t.get("entryCondition"):
                log(f"  → {t.get('entryCondition','')}", "INFO")

        log("Análisis guardado en Supabase ✓", "OK")
        log("⏱  Próximo paso: Apertura mercado 15:30 España", "INFO")

    except Exception as e:
        log(f"Error en análisis: {e}", "ERR")
        import traceback; traceback.print_exc()

# ─── TAREA 2: APERTURA MERCADO 15:30 ─────────────────────────────
def task_market_open():
    """Registra entradas al abrir el mercado"""
    global active_trades
    if not active_trades:
        log("Sin trades activos para este día", "INFO")
        return

    log("MERCADO ABIERTO — Procesando entradas", "TRADE")
    now_et = datetime.now(TZ_ET)
    for sym, trade in active_trades.items():
        if trade["status"] != "PENDING":
            continue
        # Obtener precio actual
        price = get_current_price(sym)
        if not price:
            log(f"{sym}: No se pudo obtener precio en apertura", "WARN")
            continue

        strategy = trade.get("strategy","open")
        if strategy == "open":
            # Entrar directamente en apertura al precio recomendado
            actual_entry = trade["entry"]
            trade["actual_entry"] = actual_entry
            trade["status"] = "ACTIVE"
            if trade.get("id"):
                sb_update("trades", {
                    "actual_entry_price": actual_entry,
                    "entry_time": datetime.now(TZ_SPAIN).isoformat(),
                    "status": "ACTIVE"
                }, "id", trade["id"])
            log(f"{'▲' if trade['dir']=='long' else '▼'} {sym} ENTRADA en ${actual_entry:.2f} (estrategia: OPEN)", "MONEY")

        elif strategy == "wait15":
            trade["status"] = "WAITING"
            log(f"⏱ {sym} esperando 15 min para confirmar entrada", "INFO")

        elif strategy == "wait_retrace":
            trade["status"] = "WAITING_RETRACE"
            log(f"⏳ {sym} esperando retroceso a ${trade['entry']:.2f} (actual: ${price:.2f})", "INFO")

# ─── TAREA 3: MONITOREO CADA 5 MIN ───────────────────────────────
def task_monitor():
    """Revisa precios cada 5 min y detecta target/stop"""
    global active_trades
    if not active_trades:
        return

    now_spain = datetime.now(TZ_SPAIN)
    now_et = datetime.now(TZ_ET)
    log(f"Monitoreando {len([t for t in active_trades.values() if t['status'] in ['ACTIVE','WAITING','WAITING_RETRACE']])} trades activos...")

    for sym, trade in list(active_trades.items()):
        if trade["status"] == "CLOSED":
            continue

        price = get_current_price(sym)
        if not price:
            continue

        is_long = trade["dir"] == "long"
        entry = trade.get("actual_entry") or trade["entry"]

        # ── Gestionar entradas pendientes ──────────────────────────
        if trade["status"] == "WAITING":
            # wait15: entrar si ya pasaron 15 min y precio cerca de entrada
            open_time_et = now_et.replace(hour=9, minute=45, second=0)
            if now_et >= open_time_et:
                actual_entry = price  # entrar al precio actual
                trade["actual_entry"] = actual_entry
                trade["status"] = "ACTIVE"
                if trade.get("id"):
                    sb_update("trades", {"actual_entry_price": actual_entry, "entry_time": now_spain.isoformat(), "status":"ACTIVE"}, "id", trade["id"])
                log(f"{'▲' if is_long else '▼'} {sym} ENTRADA (wait15) en ${actual_entry:.2f}", "MONEY")
            continue

        if trade["status"] == "WAITING_RETRACE":
            # Entrar cuando el precio toca el nivel de entrada recomendado
            diff_pct = abs(price - trade["entry"]) / trade["entry"] * 100
            if diff_pct <= 0.3:  # dentro del 0.3% del precio objetivo
                trade["actual_entry"] = trade["entry"]
                trade["status"] = "ACTIVE"
                if trade.get("id"):
                    sb_update("trades", {"actual_entry_price": trade["entry"], "entry_time": now_spain.isoformat(), "status":"ACTIVE"}, "id", trade["id"])
                log(f"{'▲' if is_long else '▼'} {sym} ENTRADA RETRACE en ${trade['entry']:.2f} (precio: ${price:.2f})", "MONEY")
            else:
                log(f"⏳ {sym} esperando retrace | Actual: ${price:.2f} | Objetivo: ${trade['entry']:.2f} | Diff: {diff_pct:.1f}%")
            continue

        if trade["status"] != "ACTIVE":
            continue

        # ── Calcular P&L actual ─────────────────────────────────────
        pnl_pct = ((price - entry) / entry * 100) if is_long else ((entry - price) / entry * 100)
        capital = 10000  # $10,000 por trade
        pnl_usd = (pnl_pct / 100) * capital

        log(f"{'▲' if is_long else '▼'} {sym} | Entrada: ${entry:.2f} | Actual: ${price:.2f} | P&L: {'+'if pnl_usd>=0 else ''}{pnl_usd:.0f}$ ({'+' if pnl_pct>=0 else ''}{pnl_pct:.2f}%)")

        # ── Detectar TARGET o STOP ──────────────────────────────────
        exit_reason = None
        if is_long:
            if price >= trade["target2"]:
                exit_reason = "TARGET2"
            elif price >= trade["target1"]:
                exit_reason = "TARGET1"
            elif price <= trade["stop"]:
                exit_reason = "STOP"
        else:  # short
            if price <= trade["target2"]:
                exit_reason = "TARGET2"
            elif price <= trade["target1"]:
                exit_reason = "TARGET1"
            elif price >= trade["stop"]:
                exit_reason = "STOP"

        if exit_reason:
            close_trade(sym, trade, price, pnl_pct, pnl_usd, exit_reason)

# ─── TAREA 4: CIERRE FORZADO 22:00 ───────────────────────────────
def task_market_close():
    """Cierra todos los trades abiertos al cerrar el mercado"""
    global active_trades
    log("MERCADO CERRADO — Cerrando posiciones abiertas", "TRADE")

    for sym, trade in list(active_trades.items()):
        if trade["status"] != "ACTIVE":
            continue
        price = get_current_price(sym)
        if not price:
            price = trade.get("actual_entry") or trade["entry"]
        entry = trade.get("actual_entry") or trade["entry"]
        is_long = trade["dir"] == "long"
        pnl_pct = ((price - entry) / entry * 100) if is_long else ((entry - price) / entry * 100)
        pnl_usd = (pnl_pct / 100) * 10000
        close_trade(sym, trade, price, pnl_pct, pnl_usd, "EOD")

    # Resumen del día
    task_daily_summary()

def close_trade(sym, trade, exit_price, pnl_pct, pnl_usd, reason):
    """Registra el cierre de un trade"""
    result = "WIN" if pnl_usd > 50 else "LOSS" if pnl_usd < -50 else "SCRATCH"
    icon = "✅" if result=="WIN" else "❌" if result=="LOSS" else "➖"
    log(f"{icon} {sym} CIERRE ({reason}) | Entrada: ${trade.get('actual_entry',trade['entry']):.2f} | Salida: ${exit_price:.2f} | P&L: {'+' if pnl_usd>=0 else ''}{pnl_usd:.0f}$ ({'+' if pnl_pct>=0 else ''}{pnl_pct:.2f}%) | {result}", "MONEY")
    trade["status"] = "CLOSED"
    if trade.get("id"):
        sb_update("trades", {
            "actual_exit_price": exit_price,
            "exit_time": datetime.now(TZ_SPAIN).isoformat(),
            "exit_reason": reason,
            "pnl_pct": round(pnl_pct, 2),
            "pnl_usd": round(pnl_usd, 2),
            "result": result,
            "status": "CLOSED"
        }, "id", trade["id"])

# ─── TAREA 5: RESUMEN DIARIO ──────────────────────────────────────
def task_daily_summary():
    """Muestra y guarda el resumen del día"""
    today_iso = date.today().isoformat()
    trades_hoy = sb_select("trades", params={"date": f"eq.{today_iso}", "status": "eq.CLOSED"})
    if not trades_hoy:
        log("Sin trades cerrados hoy para el resumen", "INFO")
        return

    total = len(trades_hoy)
    wins = sum(1 for t in trades_hoy if t.get("result")=="WIN")
    losses = sum(1 for t in trades_hoy if t.get("result")=="LOSS")
    total_pnl = sum(t.get("pnl_usd",0) or 0 for t in trades_hoy)
    win_rate = (wins/total*100) if total else 0

    log("═" * 55)
    log(f"RESUMEN DEL DÍA — {today_iso}", "MONEY")
    log("═" * 55)
    log(f"  Trades ejecutados: {total}")
    log(f"  Ganadores: {wins} | Perdedores: {losses} | Win rate: {win_rate:.0f}%")
    log(f"  P&L del día: {'+'if total_pnl>=0 else ''}{total_pnl:.0f}$ sobre $10.000 por trade")
    log("─" * 55)
    for t in trades_hoy:
        icon = "✅" if t.get("result")=="WIN" else "❌" if t.get("result")=="LOSS" else "➖"
        log(f"  {icon} {t.get('ticker','')} | {'+'if(t.get('pnl_usd',0) or 0)>=0 else ''}{t.get('pnl_usd',0) or 0:.0f}$ ({t.get('exit_reason','')})")
    log("═" * 55)

    # Guardar resumen en Supabase
    sb_insert("daily_summary", {
        "date": today_iso,
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate, 1),
        "total_pnl_usd": round(total_pnl, 2),
        "capital_per_trade": 10000
    })

# ─── SCHEDULER ────────────────────────────────────────────────────
def is_weekday():
    return datetime.now(TZ_SPAIN).weekday() < 5  # L-V

def guarded(fn):
    """Solo corre en días de semana"""
    def wrapper():
        if is_weekday():
            fn()
        else:
            log(f"Fin de semana — {fn.__name__} omitido", "INFO")
    return wrapper

def setup_schedule():
    # Análisis pre-market: 15:25 España (9:25 AM ET — 5 min antes apertura)
    schedule.every().monday.at("15:25").do(guarded(task_analysis))
    schedule.every().tuesday.at("15:25").do(guarded(task_analysis))
    schedule.every().wednesday.at("15:25").do(guarded(task_analysis))
    schedule.every().thursday.at("15:25").do(guarded(task_analysis))
    schedule.every().friday.at("15:25").do(guarded(task_analysis))

    # Apertura mercado: 15:30 España (9:30 AM ET)
    schedule.every().day.at("15:30").do(guarded(task_market_open))

    # Monitoreo cada 2 min durante sesión (15:32 → 21:58)
    for h in range(15, 22):
        for m in range(0, 60, 2):
            if (h == 15 and m < 32) or (h == 21 and m > 58):
                continue
            schedule.every().day.at(f"{h:02d}:{m:02d}").do(guarded(task_monitor))

    # Cierre de mercado: 22:00 España (4:00 PM ET)
    schedule.every().day.at("22:00").do(guarded(task_market_close))

    # Resumen final: 22:05
    schedule.every().day.at("22:05").do(guarded(task_daily_summary))

# ─── MAIN ─────────────────────────────────────────────────────────
def main():
    log("═" * 55)
    log("MARKET ORACLE — Railway Cloud Worker")
    log(f"Zona horaria: Europa/Madrid")
    log(f"Análisis diario: 15:25 (5 min antes apertura) | Apertura: 15:30 | Cierre: 22:00")
    log(f"Supabase: {'✓ CONECTADO' if SUPABASE_URL else '✗ NO CONFIGURADO'}")
    log(f"Anthropic: {'✓ OK' if ANTHROPIC_KEY else '✗ FALTA KEY'}")
    log("═" * 55)

    import sys
    run_now = "--ahora" in sys.argv or os.environ.get("RUN_ON_START","").lower() == "true"
    if run_now:
        log("▶ Ejecutando análisis AHORA...")
        task_analysis()
        if "--ahora" in sys.argv:
            return
        log("✓ Análisis completado — arrancando scheduler normal...")

    if "--test-monitor" in sys.argv:
        log("▶ Test de monitoreo...")
        task_monitor()
        return

    setup_schedule()
    log(f"Scheduler activo. Próxima ejecución: {schedule.next_run()}", "OK")

    while True:
        schedule.run_pending()
        time.sleep(20)

if __name__ == "__main__":
    main()
