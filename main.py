#!/usr/bin/env python3
"""
Market Oracle — Railway Cloud Worker
Autor: generado por Claude para Juan Rafael

FLUJO DIARIO (hora España / hora ET):
  15:25 / 9:25  → Análisis pre-market con IA + precios Twelve Data reales
  15:30 / 9:30  → Apertura: entradas a precio real de mercado
  c/2 min       → Monitor: detecta TARGET1, TARGET2, STOP → cierra con P&L
  22:00 / 16:00 → Cierre EOD: cierra todas las posiciones abiertas
  22:00-22:14   → Resumen diario guardado en Supabase
  Capital: $10,000 por trade | WIN: P&L>$50 | LOSS: P&L<-$50 | SCRATCH: resto
"""

import os, sys, time, json, requests
try:
    import schedule
except ImportError:
    schedule = None  # No se usa en el loop propio
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
    """Obtiene precios reales via yfinance — sin API key, sin rate limit"""
    if not tickers:
        return {}
    results = {}
    try:
        data = yf.download(tickers, period="1d", interval="1m", progress=False, auto_adjust=True)
        if data.empty:
            log("yfinance: sin datos", "WARN")
            return {}

        # Precio actual = último cierre del minuto
        close = data["Close"] if "Close" in data else data.get("close", None)
        if close is None:
            return {}

        latest = close.iloc[-1]
        prev = close.iloc[0]  # apertura del día como referencia

        # prev_close via Polygon /prev para gap correcto
        for sym in tickers:
            try:
                current = float(latest[sym]) if sym in latest else None
                if not current or str(current) == 'nan':
                    continue
                open_price = float(close.iloc[0][sym]) if sym in close.iloc[0] else current

                # prev_close via Polygon
                prev_close = 0
                try:
                    r = requests.get(
                        f"https://api.polygon.io/v2/aggs/ticker/{sym}/prev?adjusted=true&apiKey={POLYGON_KEY}",
                        timeout=8
                    )
                    res = r.json().get("results", [])
                    if res:
                        prev_close = float(res[0].get("c", 0))
                except:
                    pass

                gap_pct = round(((current - prev_close) / prev_close * 100), 2) if prev_close else 0
                results[sym] = {"current": round(current, 4), "prev_close": prev_close, "open": round(open_price, 4), "gap_pct": gap_pct}
                log(f"  {sym}: ${current:.2f} (gap {gap_pct:+.1f}% vs ayer)", "INFO")
            except Exception as e:
                log(f"  {sym}: error procesando — {e}", "WARN")

    except Exception as e:
        log(f"yfinance error: {e}", "WARN")

    return results


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

def sb_upsert(table, data, on_conflict="date"):
    """INSERT con ON CONFLICT UPDATE — evita errores de duplicado"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": f"return=representation,resolution=merge-duplicates"
    }
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    try:
        r = requests.post(url, headers=headers, json=data, timeout=15)
        return r.json() if r.ok and r.text else None
    except Exception as e:
        log(f"Supabase upsert error ({table}): {e}", "ERR")
        return None

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

def get_fmp(sym):
    """
    Datos complementarios: noticias + indicadores técnicos.
    Fuentes: Polygon (noticias) + Twelve Data (RSI/MACD) + Polygon (ticker details).
    FMP fue reemplazado — sus endpoints legacy no funcionan con el plan gratuito.
    """
    result = {
        "upcoming_earnings": False,
        "earnings_date":     None,
        "news_headlines":    [],
        "rsi":               None,
        "analyst_rating":    "neutral",
    }
    try:
        # Noticias recientes (Polygon)
        r = requests.get(
            f"https://api.polygon.io/v2/reference/news?ticker={sym}&limit=5&apiKey={POLYGON_KEY}",
            timeout=8)
        if r.ok:
            result["news_headlines"] = [n.get("title","") for n in r.json().get("results",[])[:3]]
    except: pass
    try:
        # RSI diario (Twelve Data) — gratis
        r = requests.get(
            f"https://api.twelvedata.com/rsi?symbol={sym}&interval=1day&time_period=14&outputsize=1&apikey={TWELVE_KEY}",
            timeout=8)
        if r.ok:
            vals = r.json().get("values", [])
            result["rsi"] = float(vals[0]["rsi"]) if vals else None
    except: pass
    try:
        # Ticker details — sector/industria (Polygon)
        r = requests.get(
            f"https://api.polygon.io/v3/reference/tickers/{sym}?apiKey={POLYGON_KEY}",
            timeout=8)
        if r.ok:
            res = r.json().get("results", {})
            result["sector"]   = res.get("sic_description","")
            result["market_cap"]= res.get("market_cap")
    except: pass
    return result


def get_current_price(sym):
    """Precio actual via Twelve Data /price (tiempo real) con fallback a FMP"""
    try:
        url = f"https://api.twelvedata.com/price?symbol={sym}&apikey={TWELVE_KEY}"
        r = requests.get(url, headers={"User-Agent": "market-oracle"}, timeout=10)
        data = r.json()
        price = float(data.get("price", 0) or 0)
        if price:
            return price
    except:
        pass
    # Fallback a FMP
    try:
        q = (fmp_get(f"/v3/quote/{sym}") or [{}])[0]
        return float(q.get("price", 0) or 0) or None
    except:
        return None

# ─── CLAUDE ───────────────────────────────────────────────────────
def parse_json(text):
    """Extrae y parsea JSON de la respuesta de Claude con múltiples intentos"""
    text = text.replace("```json","").replace("```","").strip()
    # Buscar el JSON más completo (desde primer { hasta último })
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1 or e <= s:
        raise ValueError("Sin JSON válido en respuesta")
    candidate = text[s:e+1]
    # Intentar parsear; si falla limpiar caracteres problemáticos
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Limpiar y reintentar
        cleaned = candidate.replace("\n", " ").replace("\t", " ")
        # Eliminar trailing commas antes de } o ]
        import re
        cleaned = re.sub(r',\s*([}\]])', r'', cleaned)
        return json.loads(cleaned)

def call_claude(system, user, max_tokens=8000):
    if not ANTHROPIC_KEY:
        raise ValueError("Falta ANTHROPIC_KEY como variable de entorno en Railway")
    for attempt in range(3):
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"Content-Type":"application/json","x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01"},
                json={
                    "model": "claude-sonnet-4-6",
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
            # Extraer texto de todos los bloques text
            text = "".join(b["text"] for b in data.get("content",[]) if b.get("type")=="text")
            result = parse_json(text)
            # Validar que el JSON no esté vacío
            if result and len(result) > 1:
                return result
            log(f"JSON vacío en intento {attempt+1}, reintentando...", "WARN")
            time.sleep(3)
        except Exception as e:
            if attempt == 2:
                raise
            log(f"call_claude intento {attempt+1} falló: {e} — reintentando...", "WARN")
            time.sleep(5)
    raise ValueError("call_claude: 3 intentos fallidos")

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
        if not d or not isinstance(d, dict): continue
        l = [f"{s}: RSI={d.get('rsi','?')}"]
        if d.get("upcoming_earnings"):
            l.append(f"  EARNINGS próximos: {d.get('earnings_date','?')}")
        news = d.get("news_headlines", [])
        if news:
            l.append(f"  NOTICIAS: {' | '.join(news[:2])}")
        if d.get("market_cap"):
            l.append(f"  MarketCap={d.get('market_cap')}")
        fc_lines.append("\n".join(l))
    fc_joined = "\n".join(fc_lines) if fc_lines else "n/d"
    earnings_today = "ver datos individuales"
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
7-ENTRADA PRECISA: gap<3%=open gap3-5%=wait15 gap>5%=wait_retrace con precio exacto. OBLIGATORIO: si no puedes calcular entryPrice concreto con los precios reales provistos descarta ese ticker y elige otro. NUNCA enviar entryPrice=0 t1=0 stop=0. CRITICO: si usas wait_retrace el entryPrice DEBE estar dentro del rango intraday real (maximo 5% por debajo del precio actual). Si el retroceso necesario es mayor al 5% desde precio actual=descarta el ticker, el gap ya esta quemado y no hay setup valido.

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
        try:
            sc = call_claude(SCREENER_PROMPT, f"Hoy {today}. Busca movers. Solo JSON con {{", 3000)
            if not sc or len(sc.get("c", [])) == 0:
                raise ValueError("Screener sin candidatos")
        except Exception as sc_err:
            log(f"Screener falló ({sc_err}), usando fallback de mercado activo", "WARN")
            # Fallback: tickers activos estándar para análisis
            sc = {
                "at": f"{now.strftime('%H:%M')} ET",
                "es": "0%", "nq": "0%", "vix": "18", "bias": "neutral",
                "c": [
                    {"t":"NVDA","n":"NVIDIA Corp","ch":"0%","w":"AI leader alta volatilidad","cat":"active"},
                    {"t":"AMD","n":"Advanced Micro Devices","ch":"0%","w":"Semiconductores activo","cat":"active"},
                    {"t":"TSLA","n":"Tesla Inc","ch":"0%","w":"EV volatilidad alta","cat":"active"},
                    {"t":"AAPL","n":"Apple Inc","ch":"0%","w":"Mega cap activo","cat":"active"},
                    {"t":"META","n":"Meta Platforms","ch":"0%","w":"AI momentum","cat":"active"},
                    {"t":"AMZN","n":"Amazon","ch":"0%","w":"Tech activo","cat":"active"},
                    {"t":"MSFT","n":"Microsoft","ch":"0%","w":"Cloud AI","cat":"active"},
                    {"t":"GOOGL","n":"Alphabet","ch":"0%","w":"Search AI","cat":"active"},
                ],
                "earn": [], "news": "Mercado en sesion normal"
            }
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
            f"{s}: PRECIO_ACTUAL=${d['current']} ({'+' if d['gap_pct']>=0 else ''}{d['gap_pct']}% vs cierre_ayer) prev_close=${d['prev_close']}"
            + (f" open=${d['open']}" if d.get('open') else "")
            + (f" PRE-MARKET=${d['pre_market']}" if d.get('pre_market') else "")
            for s, d in td_prices.items()
        ) or "No disponible"
        analysis = call_claude(
            build_prompt(poly, fmp),
            f"Hoy {today}. Candidatas: {list_str}. ES={sc.get('es','?')} NQ={sc.get('nq','?')} VIX={sc.get('vix','?')}. Earnings:{fmp.get('_today','ninguno')}.\n\nPRECIOS EN TIEMPO REAL — OBLIGATORIO usar estos precios para calcular entradas targets y stops:\n{td_str}\n\nSolo JSON con {{",
            10000
        )
        trades = analysis.get("trades", [])
        log(f"→ {len(trades)} trades recomendados | VIX: {analysis.get('vix','?')} | Entorno: {analysis.get('env','?').upper()}", "OK")

        # Guardar análisis diario en Supabase
        today_iso = date.today().isoformat()
        da_result = sb_upsert("daily_analysis", {
            "date": today_iso,
            "analysis_time": datetime.now(TZ_SPAIN).isoformat(),
            "vix": analysis.get("vix"),
            "spx_futures": analysis.get("spx"),
            "nq_futures": sc.get("nq"),
            "es_futures": sc.get("es"),
            "environment": analysis.get("env"),
            "score": analysis.get("score"),
            "market_bias": sc.get("bias"),
            "summary": analysis.get("summary","")[:500],
            "screened_count": len(tickers),
            "trades_count": len(trades),
            "td_prices_json": json.dumps(td_prices),
            "full_json": json.dumps(analysis)[:3000]
        })
        if not da_result:
            log("⚠️  daily_analysis INSERT falló — revisa columnas en Supabase", "WARN")

        # Guardar cada trade recomendado
        global active_trades
        active_trades = {}
        for t in trades:
            sym = t.get("t","")
            if not sym: continue
            # Validar que el trade tiene precios definidos
            if not t.get("entryPrice") or float(t.get("entryPrice", 0)) <= 0:
                log(f"  {sym}: sin precio de entrada — trade descartado", "WARN")
                continue
            # Validar wait_retrace: entryPrice no puede estar más de 5% por debajo del precio actual
            if t.get("entryStrategy") == "wait_retrace":
                precio_actual = float(td_prices.get(sym, {}).get("current") or 0)
                entry_price = float(t.get("entryPrice", 0))
                if precio_actual > 0 and entry_price > 0:
                    diff_pct = (precio_actual - entry_price) / precio_actual * 100
                    if diff_pct > 5:
                        log(f"  {sym}: wait_retrace requiere caída {diff_pct:.1f}% — gap quemado, descartado", "WARN")
                        continue
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
                "rec_target1": round(float(t.get("entryPrice",0)) * 1.03, 2),   # T1 fijo +3%
                "rec_target2": None,  # T2 eliminado — cierre único en T1
                "rec_stop": round(float(t.get("entryPrice",0)) * 0.985, 2),  # Stop fijo -1.5%
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
                "status": "PENDING",   # PENDING | ACTIVE | CLOSED
                # Precio real de Twelve Data en el momento del análisis
                "price_at_analysis": td_prices.get(sym, {}).get("current"),
                "gap_pct_realtime": td_prices.get(sym, {}).get("gap_pct"),
                "open_price_day": td_prices.get(sym, {}).get("open"),
                "prev_close_day": td_prices.get(sym, {}).get("prev_close"),
            }
            trade_insert = sb_insert("trades", rec_data)
            if not trade_insert:
                log(f"⚠️  {sym}: INSERT en trades falló — revisa schema Supabase", "WARN")
            rec_id = trade_insert[0]["id"] if trade_insert else None

            # Preparar para seguimiento durante sesión — T1/T2 calculados en código
            entry_p = float(t.get("entryPrice", 0))
            t1_calc = round(entry_p * 1.03, 2)
            active_trades[sym] = {
                "id": rec_id,
                "dir": t.get("dir","long"),
                "entry": entry_p,
                "target1": t1_calc,
                "target2": None,  # eliminado
                "stop": round(entry_p * 0.985, 2),  # Stop fijo -1.5%
                "strategy": t.get("entryStrategy","open"),
                "status": "PENDING",
                "actual_entry": None
            }

            # Mostrar en consola
            gap_icon = " ⚠️ GAP TRAMPA" if t.get("gapTrap") else ""
            reg_icon = " 🏛" if t.get("regulatoryRisk") else ""
            stop_calc = round(entry_p * 0.985, 2)
            log(f"{'▲' if t.get('dir')=='long' else '▼'} {sym} ({t.get('prob',0)}% {t.get('label','')}) | Entrada: ${entry_p} | T1: ${t1_calc} (+3%) | Stop: ${stop_calc} (-1.5%){gap_icon}{reg_icon}", "TRADE")
            if t.get("entryCondition"):
                log(f"  → {t.get('entryCondition','')}", "INFO")

        saved_count = sum(1 for t in active_trades.values() if t.get("id"))
        if saved_count > 0:
            log(f"Análisis guardado en Supabase ✓ ({saved_count}/{len(trades)} trades)", "OK")
        else:
            log("⚠️  Trades NO guardados en Supabase — verifica SUPABASE_KEY", "WARN")
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
            # Entrar al precio REAL de apertura (Twelve Data)
            actual_entry = price  # precio real del mercado ahora mismo
            shares = round(10000 / actual_entry, 4) if actual_entry and actual_entry > 0 else 0 if actual_entry else 0
            trade["actual_entry"] = actual_entry
            trade["shares"] = shares
            trade["status"] = "ACTIVE"
            if trade.get("id"):
                sb_update("trades", {
                    "actual_entry_price": actual_entry,
                    "entry_time": datetime.now(TZ_SPAIN).isoformat(),
                    "status": "ACTIVE"
                }, "id", trade["id"])
            log(f"{'▲' if trade['dir']=='long' else '▼'} {sym} ENTRADA REAL en ${actual_entry:.2f} | {shares:.2f} acciones | Capital: $10,000", "MONEY")

        elif strategy == "wait15":
            trade["status"] = "WAITING"
            log(f"⏱ {sym} esperando 15 min para confirmar entrada", "INFO")

        elif strategy == "wait_retrace":
            trade["status"] = "WAITING_RETRACE"
            log(f"⏳ {sym} esperando retroceso a ${trade['entry']:.2f} (actual: ${price:.2f})", "INFO")

# ─── TAREA 3: MONITOREO CADA 2 MIN ───────────────────────────────
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

        # ── PENDING pasado el open window (15:34): procesar como market_open ─
        if trade["status"] == "PENDING":
            now_et = datetime.now(TZ_ET)
            past_open = now_et.hour > 9 or (now_et.hour == 9 and now_et.minute >= 34)
            if past_open and price:
                strategy = trade.get("strategy", "open")
                if strategy == "open":
                    trade["actual_entry"] = price
                    trade["shares"] = round(10000 / price, 4) if price and price > 0 else 0
                    trade["status"] = "ACTIVE"
                    if trade.get("id"):
                        sb_update("trades", {"actual_entry_price": price, "entry_time": datetime.now(TZ_SPAIN).isoformat(), "status": "ACTIVE"}, "id", trade["id"])
                    log(f"▲ {sym} ENTRADA (post-open) en ${price:.2f} | {trade['shares']:.2f} acc | $10,000", "MONEY")
                elif strategy == "wait15":
                    trade["status"] = "WAITING"
                    if trade.get("id"):
                        sb_update("trades", {"status": "WAITING"}, "id", trade["id"])
                    log(f"⏱ {sym} → WAITING (wait15)", "INFO")
                elif strategy == "wait_retrace":
                    trade["status"] = "WAITING_RETRACE"
                    if trade.get("id"):
                        sb_update("trades", {"status": "WAITING_RETRACE"}, "id", trade["id"])
                    log(f"⏳ {sym} → WAITING_RETRACE a ${trade['entry']:.2f}", "INFO")
            continue

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
                shares = round(10000 / actual_entry, 4) if actual_entry and actual_entry > 0 else 0 if actual_entry else 0
                trade["shares"] = shares
                log(f"{'▲' if is_long else '▼'} {sym} ENTRADA WAIT15 en ${actual_entry:.2f} | {shares:.2f} acciones | Capital: $10,000", "MONEY")
            continue

        if trade["status"] == "WAITING_RETRACE":
            # Entrar cuando el precio toca el nivel de entrada recomendado
            if not trade.get("entry") or trade["entry"] <= 0:
                log(f"  {sym}: entry=0 en WAITING_RETRACE — saltando", "WARN")
                continue
            diff_pct = abs(price - trade["entry"]) / trade["entry"] * 100
            if diff_pct <= 0.3:  # dentro del 0.3% del precio objetivo
                trade["actual_entry"] = trade["entry"]
                trade["status"] = "ACTIVE"
                if trade.get("id"):
                    sb_update("trades", {"actual_entry_price": trade["entry"], "entry_time": now_spain.isoformat(), "status":"ACTIVE"}, "id", trade["id"])
                shares = round(10000 / trade["entry"], 4)
                trade["shares"] = shares
                log(f"{'▲' if is_long else '▼'} {sym} ENTRADA RETRACE en ${trade['entry']:.2f} | {shares:.2f} acciones | Capital: $10,000", "MONEY")
            else:
                log(f"⏳ {sym} esperando retrace | Actual: ${price:.2f} | Objetivo: ${trade['entry']:.2f} | Diff: {diff_pct:.1f}%")
            continue

        if trade["status"] != "ACTIVE":
            continue

        # ── Calcular P&L actual ─────────────────────────────────────
        if not entry or entry <= 0:
            log(f"{sym}: entry=0, skip monitor", "WARN")
            continue
        pnl_pct = ((price - entry) / entry * 100) if is_long else ((entry - price) / entry * 100)
        capital = 10000  # $10,000 por trade
        pnl_usd = (pnl_pct / 100) * capital

        log(f"{'▲' if is_long else '▼'} {sym} | Entrada: ${entry:.2f} | Actual: ${price:.2f} | P&L: {'+'if pnl_usd>=0 else ''}{pnl_usd:.0f}$ ({'+' if pnl_pct>=0 else ''}{pnl_pct:.2f}%)")

        # ── Detectar TARGET o STOP ──────────────────────────────────
        exit_reason = None
        if is_long:
            if trade.get("target1") and price >= trade["target1"]:
                exit_reason = "TARGET1"
            elif trade.get("stop") and price <= trade["stop"]:
                exit_reason = "STOP"
        else:  # short
            if trade.get("target1") and price <= trade["target1"]:
                exit_reason = "TARGET1"
            elif trade.get("stop") and price >= trade["stop"]:
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
    """Registra el cierre de un trade con P&L sobre $10,000"""
    result = "WIN" if pnl_usd > 50 else "LOSS" if pnl_usd < -50 else "SCRATCH"
    icon = "✅" if result=="WIN" else "❌" if result=="LOSS" else "➖"
    entry = trade.get("actual_entry") or trade["entry"]
    shares = trade.get("shares") or round(10000 / entry, 4) if entry else 0
    log(f"{icon} {sym} CIERRE ({reason})", "MONEY")
    log(f"   Entrada: ${entry:.2f} | Salida: ${exit_price:.2f} | Acciones: {shares:.2f}", "MONEY")
    log(f"   Capital: $10,000 | P&L: {'+' if pnl_usd>=0 else ''}{pnl_usd:.2f}$ ({'+' if pnl_pct>=0 else ''}{pnl_pct:.2f}%) | {result}", "MONEY")
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
    """Cierra todos los trades abiertos al EOD y guarda el resumen del día"""
    global active_trades
    today_iso = date.today().isoformat()

    # ── PASO 1: Cerrar todos los trades que quedaron abiertos ─────
    log("CIERRE EOD — cerrando posiciones abiertas...", "TRADE")
    for sym, trade in list(active_trades.items()):
        if trade["status"] == "CLOSED":
            continue

        price_eod = get_current_price(sym)

        if trade["status"] in ("WAITING", "WAITING_RETRACE", "PENDING"):
            # Nunca entró — cerrar sin P&L
            log(f"➖ {sym} EOD_NO_ENTRY — nunca alcanzó la entrada", "TRADE")
            trade["status"] = "CLOSED"
            if trade.get("id"):
                sb_update("trades", {
                    "exit_reason": "EOD_NO_ENTRY",
                    "pnl_pct": 0,
                    "pnl_usd": 0,
                    "result": "SCRATCH",
                    "status": "CLOSED",
                    "exit_time": datetime.now(TZ_SPAIN).isoformat()
                }, "id", trade["id"])

        elif trade["status"] == "ACTIVE" and price_eod:
            # Trade activo — cerrar al precio actual
            entry = trade.get("actual_entry") or trade["entry"]
            is_long = trade.get("dir", "long") == "long"
            pnl_pct = ((price_eod - entry) / entry * 100) if is_long else ((entry - price_eod) / entry * 100)
            pnl_usd = (pnl_pct / 100) * 10000
            close_trade(sym, trade, price_eod, pnl_pct, pnl_usd, "EOD")

    # ── PASO 2: Calcular resumen con todos los trades ──────────────
    trades_hoy = sb_select("trades", params={"date": f"eq.{today_iso}", "status": "eq.CLOSED", "select": "*"}) or []
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
    scratches = total - wins - losses
    best = max(trades_hoy, key=lambda t: t.get("pnl_usd",0) or 0, default={})
    worst = min(trades_hoy, key=lambda t: t.get("pnl_usd",0) or 0, default={})
    ds_result = sb_upsert("daily_summary", {
        "date": today_iso,
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "scratches": scratches,
        "win_rate_pct": round(win_rate, 1),
        "total_pnl_usd": round(total_pnl, 2),
        "avg_pnl_usd": round(total_pnl / total, 2) if total else 0,
        "best_trade": best.get("ticker",""),
        "best_pnl_usd": best.get("pnl_usd", 0),
        "worst_trade": worst.get("ticker",""),
        "worst_pnl_usd": worst.get("pnl_usd", 0),
        "capital_per_trade": 10000,
        "notes": ""
    })
    if not ds_result:
        log("⚠️  daily_summary INSERT falló", "WARN")
    else:
        log("Resumen del día guardado en Supabase ✓", "OK")

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

def reload_trades_from_supabase():
    """Recarga desde Supabase los trades de hoy que NO están cerrados (por restart)"""
    global active_trades
    today_iso = date.today().isoformat()
    pendientes = sb_select("trades", params={
        "date": f"eq.{today_iso}",
        "select": "*"
    }) or []
    # Filtrar solo los no cerrados
    abiertos = [t for t in pendientes if t.get("status") not in ("CLOSED",)]
    if not abiertos:
        log("Sin trades abiertos de hoy para recargar", "INFO")
        return
    for t in abiertos:
        sym = t.get("ticker")
        if not sym or sym in active_trades:
            continue
        entry_val = float(t.get("actual_entry_price") or t.get("rec_entry_price") or 0)
        # T1/T2: usar Supabase si están definidos, sino recalcular +2% y +3.5%
        t1_val = float(t.get("rec_target1") or 0)
        if entry_val > 0 and t1_val == 0:
            t1_val = round(entry_val * 1.03, 2)
            log(f"  {sym}: T1 recalculado en reload — T1=${t1_val}", "INFO")
        active_trades[sym] = {
            "id": t["id"],
            "dir": t.get("direction", "long"),
            "entry": entry_val,
            "target1": t1_val,
            "target2": None,  # eliminado
            "stop": float(t.get("rec_stop") or 0),
            "strategy": t.get("entry_strategy", "open"),
            "status": t.get("status", "PENDING"),
            "actual_entry": float(t.get("actual_entry_price") or 0) or None,
            "shares": round(10000 / float(t.get("actual_entry_price") or t.get("rec_entry_price") or 1), 4)
        }
    log(f"Recargados {len(abiertos)} trades de Supabase: {[t['ticker'] for t in abiertos]}", "OK")


def main():
    log("=" * 55)
    log("MARKET ORACLE — Railway Cloud Worker")
    log("Zona horaria: Europa/Madrid")
    log("Analisis: 15:25 | Apertura: 15:30 | Cierre: 22:00 | Monitor: c/2 min")
    log("Supabase: OK" if SUPABASE_URL else "Supabase: NO CONFIGURADO")
    log("Anthropic: OK" if ANTHROPIC_KEY else "Anthropic: FALTA KEY")
    log("=" * 55)

    import sys
    run_now = "--ahora" in sys.argv or os.environ.get("RUN_ON_START","").lower() == "true"
    if run_now:
        log("Ejecutando analisis AHORA (RUN_ON_START)...")
        task_analysis()
        if "--ahora" in sys.argv:
            return
        log("Analisis completado — arrancando loop...")

    last_analysis_date  = None
    last_open_date      = None
    last_summary_date   = None
    last_heartbeat_min  = -1
    last_monitor_min    = -1

    now0 = datetime.now(TZ_SPAIN)
    log(f"Loop activo — hora Espana: {now0.strftime('%d/%m/%Y %H:%M:%S')}", "OK")
    log(f"Dia semana: {now0.weekday()} (0=Lun 4=Vie 5=Sab 6=Dom) | mercado={now0.weekday() < 5}", "OK")

    # Recargar trades activos de hoy desde Supabase (por si hubo restart)
    reload_trades_from_supabase()

    while True:
        try:
            now = datetime.now(TZ_SPAIN)
            today = now.date()
            wd = now.weekday()   # 0=Lun ... 4=Vie ... 6=Dom
            h, m = now.hour, now.minute

            # Solo L-V (dias de mercado americano)
            if wd < 5:

                # 15:25-15:29 — Analisis pre-market (una vez por dia)
                if h == 15 and 25 <= m < 30 and last_analysis_date != today:
                    last_analysis_date = today
                    log(f"[{now.strftime('%H:%M')}] INICIANDO ANALISIS PRE-MARKET")
                    guarded(task_analysis)()

                # 15:30-15:34 — Apertura mercado (una vez por dia)
                if h == 15 and 30 <= m < 35 and last_open_date != today:
                    last_open_date = today
                    log(f"[{now.strftime('%H:%M')}] APERTURA MERCADO")
                    guarded(task_market_open)()

                # 15:32-21:59 — Monitoreo cada 2 min
                en_sesion = (h > 15 or (h == 15 and m >= 32)) and h < 22
                if en_sesion and m % 2 == 0 and m != last_monitor_min:
                    last_monitor_min = m
                    guarded(task_monitor)()

                # 22:00-22:14 — Cierre + resumen (una vez por dia)
                if h == 22 and 0 <= m < 15 and last_summary_date != today:
                    last_summary_date = today
                    log(f"[{now.strftime('%H:%M')}] CIERRE + RESUMEN DIARIO")
                    guarded(task_daily_summary)()

            # Heartbeat cada 30 min para verificar que el proceso vive
            if m % 30 == 0 and m != last_heartbeat_min:
                last_heartbeat_min = m
                dias = ["Lun","Mar","Mie","Jue","Vie","Sab","Dom"]
                log(f"HEARTBEAT {dias[wd]} {now.strftime('%d/%m %H:%M')} Espana | mercado={wd < 5}", "OK")

        except Exception as e:
            log(f"ERROR loop principal: {e}", "ERR")

        time.sleep(20)


if __name__ == "__main__":
    main()
