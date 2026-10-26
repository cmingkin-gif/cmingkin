import time
import re
import os
import json
from datetime import datetime, timedelta
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import concurrent.futures
import urllib3

urllib3.disable_warnings()

# ⚡ 強制鎖定 Python 執行環境為香港時間 (UTC+8)
os.environ["TZ"] = "Asia/Hong_Kong"
if hasattr(time, "tzset"):
    time.tzset()

# === 認證資訊 ===
TELEGRAM_TOKEN = os.environ.get("TG_TOKEN")
CHAT_ID = os.environ.get("TG_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")  # 🤖 AI 分析師鎖匙

USE_TITAN_KELLY_MODE = True
STATE_FILE = "recorded_state.json"
recorded_matches = {}

# 🎯 平博識別關鍵字
PINNACLE_KEYWORDS = ('平博', 'Pinnacle', 'Pinna', '平*')

GAME_ARRAY_RE = re.compile(r'game=Array\((.*?)\);', re.DOTALL)
GAME_ITEM_RE = re.compile(r'"(.*?)"')
TIME_EXTRACT_RE = re.compile(r'(\d{2}:\d{2})')
TR_ID_RE = re.compile(r'^tr1_')
FINISHED_KEYWORDS = ("完", "推", "消", "斬", "斩", "斷", "断", "待", "異", "异", "FT", "End")

def create_robust_session():
    s = requests.Session()
    s.headers.update({
        "Referer": "https://live.titan007.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Connection": "keep-alive"
    })
    retry_strategy = Retry(total=2, backoff_factor=0.3, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
    adapter = HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=retry_strategy)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

session = create_robust_session()

def load_state():
    global recorded_matches
    if not os.path.isfile(STATE_FILE): return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
        for m_id, data in raw_data.items():
            recorded_matches[m_id] = {
                "state": data["state"],
                "live_str": data["live_str"],
                "updated_at": datetime.fromisoformat(data["updated_at"])
            }
    except Exception:
        pass

def save_state():
    try:
        serializable = {
            m_id: {
                "state": data["state"],
                "live_str": data["live_str"],
                "updated_at": data["updated_at"].isoformat()
            }
            for m_id, data in recorded_matches.items()
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    chunks = [text[i:i+3800] for i in range(0, len(text), 3800)]
    for chunk in chunks:
        payload = {"chat_id": CHAT_ID, "text": chunk}
        for _ in range(2):
            try:
                if requests.post(url, json=payload, timeout=10).status_code == 200: break
            except Exception:
                time.sleep(1)

def cleanup_old_records(current_time):
    expired_ids = [m_id for m_id, data in recorded_matches.items() if current_time - data.get('updated_at', current_time) > timedelta(hours=24)]
    for m_id in expired_ids: del recorded_matches[m_id]

# 📐 亞盤轉換邏輯 (主隊讓球為正數，受讓為負數)
def parse_asian_handicap(handicap_str):
    h_str = str(handicap_str).replace(" ", "")
    if not h_str: return ""
    if h_str == "平手": return "0"

    is_receive = False
    if "受让" in h_str:
        is_receive = True
        h_str = h_str.replace("受让", "")
    elif "让" in h_str:
        is_receive = False
        h_str = h_str.replace("让", "")
        
    sign = "-" if is_receive else ""  
    mapping = {
        "平手/半球": "0.25", "平/半": "0.25", "半球": "0.5", "半球/一球": "0.75", "半/一": "0.75",
        "一球": "1", "一球/球半": "1.25", "一/球半": "1.25", "球半": "1.5", "球半/两球": "1.75", "球半/两": "1.75",
        "两球": "2", "两球/两球半": "2.25", "两/两半": "2.25", "两球半": "2.5", "两球半/三球": "2.75", "两半/三": "2.75",
        "三球": "3", "三球/三球半": "3.25", "三/三半": "3.25", "三球半": "3.5", "三球半/四球": "3.75", "三半/四": "3.75",
        "四球": "4", "四球/四球半": "4.25", "四/四半": "4.25", "四球半": "4.5", "四球半/五球": "4.75", "四半/五": "4.75", "五球": "5"
    }
    val = mapping.get(h_str)
    return f"{sign}{val}" if val else handicap_str

# 🤖 AI 盤口分析引擎 (升級自檢版)
def get_ai_analysis(league, home, away, init_k, live_k, hits_str, asian_info):
    if not GEMINI_API_KEY: 
        print("⚠️ 找不到 GEMINI_API_KEY")
        return ""
    
    # 🎯 已經鎖定最適合 7x24 運作嘅 gemini-2.5-flash
   model_name = "gemini-flash-latest"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
    
    prompt = f"""
    你是一個專業、冷靜且敏銳的足球盤口分析師。你的任務是透過莊家數據判斷真實意圖，找出是否有誘盤或避險動作。
    
    【賽事資訊】
    賽事: {league} | 主隊: {home} vs 客隊: {away}
    
    【盤口變化數據】
    歐賠凱利指數: 初盤[{init_k}] ➔ 即場[{live_k}]
    最低項資金流向: {hits_str}
    平博亞讓盤口: {asian_info} (註: 正數代表主隊讓球，負數代表主隊受讓)
    
    【分析任務】
    請簡潔俐落地進行分析。字數嚴格限制在 80 字以內，不要廢話，直接給出結論。格式必須如下：
    🎯 綜合勝出預測：[主勝 / 客勝 / 和局]，信心度：[⭐ 到 ⭐⭐⭐⭐⭐]
    📝 莊家意圖：[一句話總結歐亞走勢是否一致，莊家是在真實避險還是故意誘盤]
    """
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 150}
    }
    
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            data = res.json()
            analysis_text = data['candidates'][0]['content']['parts'][0]['text'].strip()
            return f"\n💡 【AI 盤口分析】\n{analysis_text}"
            
        elif res.status_code == 404:
            print(f"⚠️ API 錯誤 (404): 找不到模型 '{model_name}'。")
            print("🔄 正在向 Google 查詢你個帳號專屬嘅可用模型清單...")
            # 絕招：向 Google API 查詢可用模型
            list_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
            list_res = requests.get(list_url, timeout=10)
            if list_res.status_code == 200:
                models_data = list_res.json()
                # 過濾並抽取出所有支援生成內容嘅模型名
                available_models = [m['name'].replace('models/', '') for m in models_data.get('models', []) if 'generateContent' in m.get('supportedGenerationMethods', [])]
                print(f"✅ 查詢成功！請將代碼第 106 行嘅 model_name 更改為以下其中一個：\n{available_models}")
            else:
                print(f"⚠️ 無法獲取模型列表，狀態碼: {list_res.status_code}")
            return ""
            
        else:
            print(f"⚠️ API 錯誤 ({res.status_code}): {res.text}")
            return ""
            
    except Exception as e:
        print(f"⚠️ AI 請求出錯: {e}")
        return ""

def process_single_match(match_info):
    match_id = match_info['match_id']
    odds_url = f"https://1x2d.titan007.com/{match_id}.js?t={int(time.time()*1000)}"
    try:
        res = session.get(odds_url, timeout=(3.5, 4.5))
        if res.status_code != 200: return None
        res.encoding = 'utf-8'
        js_text = res.text
    except Exception: return None
        
    game_data_match = GAME_ARRAY_RE.search(js_text)
    if not game_data_match: return None
    games = GAME_ITEM_RE.findall(game_data_match.group(1))
    
    pinna_row = None
    sumInitProbH, sumInitProbD, sumInitProbA, init_count = 0.0, 0.0, 0.0, 0
    sumLiveProbH, sumLiveProbD, sumLiveProbA, live_count = 0.0, 0.0, 0.0, 0
    
    for g_item in games:
        f = g_item.split('|')
        if len(f) < 13: continue
        if any(k in f[2] for k in PINNACLE_KEYWORDS): pinna_row = f
        try:
            ih, id_odds, ia = float(f[3]), float(f[4]), float(f[5])
            if ih > 0 and id_odds > 0 and ia > 0:
                init_ret = 1.0 / (1.0/ih + 1.0/id_odds + 1.0/ia)
                sumInitProbH += init_ret / ih
                sumInitProbD += init_ret / id_odds
                sumInitProbA += init_ret / ia
                init_count += 1
            lh, ld, la = float(f[10] or ih), float(f[11] or id_odds), float(f[12] or ia)
            if lh > 0 and ld > 0 and la > 0:
                live_ret = 1.0 / (1.0/lh + 1.0/ld + 1.0/la)
                sumLiveProbH += live_ret / lh
                sumLiveProbD += live_ret / ld
                sumLiveProbA += live_ret / la
                live_count += 1
        except Exception: continue
            
    if not pinna_row or init_count == 0 or live_count == 0: return None
    
    try:
        pInitH, pInitD, pInitA = float(pinna_row[3]), float(pinna_row[4]), float(pinna_row[5])
        pLiveH, pLiveD, pLiveA = float(pinna_row[10] or pInitH), float(pinna_row[11] or pInitD), float(pinna_row[12] or pInitA)
        
        avgLiveProbH, avgLiveProbD, avgLiveProbA = sumLiveProbH / live_count, sumLiveProbD / live_count, sumLiveProbA / live_count
        if USE_TITAN_KELLY_MODE:
            avgInitProbH, avgInitProbD, avgInitProbA = avgLiveProbH, avgLiveProbD, avgLiveProbA
        else:
            avgInitProbH, avgInitProbD, avgInitProbA = sumInitProbH / init_count, sumInitProbD / init_count, sumInitProbA / init_count
        
        kInitH, kInitD, kInitA = round(pInitH * avgInitProbH, 2), round(pInitD * avgInitProbD, 2), round(pInitA * avgInitProbA, 2)
        kLiveH, kLiveD, kLiveA = round(pLiveH * avgLiveProbH, 2), round(pLiveD * avgLiveProbD, 2), round(pLiveA * avgLiveProbA, 2)
        
        minInitK = min(kInitH, kInitD, kInitA)
        hits, is_qualifying = [], False
        
        if kInitH == minInitK and kLiveH < kInitH and pLiveH < pInitH: hits.append(f"主({kInitH:.2f}➔{kLiveH:.2f})")
        if kInitD == minInitK and kLiveD < kInitD and pLiveD < pInitD: hits.append(f"和({kInitD:.2f}➔{kLiveD:.2f})")
        if kInitA == minInitK and kLiveA < kInitA and pLiveA < pInitA: hits.append(f"客({kInitA:.2f}➔{kLiveA:.2f})")
            
        if hits: is_qualifying = True
        
        return {
            'match_id': match_id,
            'initStr': f"{kInitH:.2f}/{kInitD:.2f}/{kInitA:.2f}",
            'liveStr': f"{kLiveH:.2f}/{kLiveD:.2f}/{kLiveA:.2f}",
            'hits_str': " ".join(hits) if hits else "--",
            'is_qualifying': is_qualifying,
            'info': match_info
        }
    except Exception: return None

def fetch_asian_odds_with_selenium(qualified_results):
    if not qualified_results: return
    
    options = Options()
    options.page_load_strategy = 'eager'
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    
    driver = None
    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(20)
        
        for r in qualified_results:
            match_id = r['match_id']
            asian_str, asian_line_change = "--", ""
            
            try:
                driver.get(f"https://vip.titan007.com/AsianOdds_n.aspx?id={match_id}&l=0")
                time.sleep(3) 
                
                soup_a = BeautifulSoup(driver.page_source, 'html.parser')
                found = False
                for tr in soup_a.find_all('tr'):
                    tds = [td.get_text(strip=True) for td in tr.find_all('td')]
                    if any(k in t for t in tds for k in PINNACLE_KEYWORDS):
                        n_idx = next(i for i, v in enumerate(tds) if any(k in v for k in PINNACLE_KEYWORDS))
                        data_tds = [t for t in tds[n_idx+1:] if t not in ('', '+', '荐', '封', '走', '早')]
                        if len(data_tds) >= 6:
                            init_h, init_l, init_a = data_tds[0], data_tds[1], data_tds[2]
                            live_h, live_l, live_a = data_tds[3], data_tds[4], data_tds[5]
                            
                            init_l_num = parse_asian_handicap(init_l)
                            live_l_num = parse_asian_handicap(live_l)
                            
                            asian_str = f"初[{init_h}/{init_l_num}/{init_a}] ➔ 終[{live_h}/{live_l_num}/{live_a}]"
                            asian_line_change = f"(盤變: {init_l_num} ➔ {live_l_num})" if init_l_num != live_l_num else ""
                            found = True
                            break
            except Exception:
                pass
            
            r['asian_str'] = asian_str
            r['asian_line_change'] = asian_line_change
            
    finally:
        if driver:
            try: driver.quit()
            except: pass

def fetch_main_page_html():
    options = Options()
    options.page_load_strategy = 'eager'
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    
    for attempt in range(1, 4):
        driver = None
        try:
            driver = webdriver.Chrome(options=options)
            driver.set_page_load_timeout(20)
            driver.get("https://live.titan007.com/indexall_big.aspx")
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "//tr[starts-with(@id, 'tr1_')]")))
            html = driver.page_source
            if html and "tr1_" in html: return html
        except Exception:
            pass
        finally:
            if driver:
                try: driver.quit()
                except: pass
        time.sleep(2)
    return ""

def run_scraper_once():
    start_ts = time.time()
    current_scan_time = datetime.now()
    cleanup_old_records(current_scan_time)

    page_html = fetch_main_page_html()
    if not page_html: return

    try:
        soup = BeautifulSoup(page_html, 'html.parser')
        rows = soup.find_all('tr', id=TR_ID_RE)
        
        match_infos = []
        for tr in rows:
            if str(tr.get('state', '')).strip().startswith('-'): continue
            match_id = tr['id'].replace('tr1_', '')
            tds = tr.find_all('td')
            status_text = (tr.find(id=f"time_{match_id}") or (tds[3] if len(tds)>3 else None))
            status_text = status_text.text.strip() if status_text else ""
            if any(k in status_text for k in FINISHED_KEYWORDS): continue

            home = (tr.find(id=f"team1_{match_id}").text.strip() if tr.find(id=f"team1_{match_id}") else "")
            away = (tr.find(id=f"team2_{match_id}").text.strip() if tr.find(id=f"team2_{match_id}") else "")
            
            raw_match_time = (tr.find(id=f"mt_{match_id}").text.strip() if tr.find(id=f"mt_{match_id}") else (tds[2].text.strip() if len(tds)>2 else ""))
            time_m = TIME_EXTRACT_RE.search(raw_match_time)
            match_dt = current_scan_time
            formatted_time = raw_match_time
            if time_m:
                hour, minute = map(int, time_m.group(1).split(':'))
                match_dt = current_scan_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if match_dt > current_scan_time + timedelta(hours=12): match_dt -= timedelta(days=1)
                elif match_dt < current_scan_time - timedelta(hours=12): match_dt += timedelta(days=1)
                formatted_time = match_dt.strftime('%Y-%m-%d %H:%M')

            if match_dt + timedelta(minutes=115) < current_scan_time: continue
                
            match_infos.append({'match_id': match_id, 'league': tds[1].text.strip() if len(tds)>1 else "", 'time': formatted_time, 'match_dt': match_dt, 'home': home, 'away': away})
            
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=100) as executor:
            future_to_match = {executor.submit(process_single_match, info): info for info in match_infos}
            for future in concurrent.futures.as_completed(future_to_match, timeout=45):
                res = future.result()
                if res: results.append(res)
        
        qualified_results = [r for r in results if r['is_qualifying']]
        fetch_asian_odds_with_selenium(qualified_results)
                    
        valid_alerts = []
        for r in results:
            m_id, is_qualifying, live_str = r['match_id'], r['is_qualifying'], r['liveStr']
            if is_qualifying:
                asian_str = r.get('asian_str', '--')
                asian_line_change = r.get('asian_line_change', '')
                full_asian_info = f"{asian_str} {asian_line_change}".strip() if asian_str != "--" else "暫無平博數據"
                
                msg = f"🏆 {r['info']['league']} | ⏱ {r['info']['time']}\n"
                msg += f"⚽ {r['info']['home']} vs {r['info']['away']}\n"
                msg += f"📊 初[{r['initStr']}] ➔ 即[{live_str}]\n"
                msg += f"🔻 最低項跌: {r['hits_str']}\n"
                msg += f"📐 亞讓: {full_asian_info}"

                # 🤖 呼叫 AI 進行分析
                ai_reply = get_ai_analysis(
                    league=r['info']['league'], 
                    home=r['info']['home'], 
                    away=r['info']['away'], 
                    init_k=r['initStr'], 
                    live_k=live_str, 
                    hits_str=r['hits_str'], 
                    asian_info=full_asian_info
                )
                if ai_reply:
                    msg += f"\n{ai_reply}"
                    time.sleep(1) # 稍作停頓防 Rate Limit

                valid_alerts.append({'match_dt': r['info']['match_dt'], 'msg': msg})
                
                prev = recorded_matches.get(m_id, {})
                if not prev or prev.get('state') == 'unqualifying' or prev.get('live_str') != live_str:
                    recorded_matches[m_id] = {'state': 'qualifying', 'live_str': live_str, 'updated_at': current_scan_time}
                else: recorded_matches[m_id]['updated_at'] = current_scan_time
            else:
                if m_id in recorded_matches and recorded_matches[m_id]['state'] == 'qualifying':
                    recorded_matches[m_id] = {'state': 'unqualifying', 'live_str': live_str, 'updated_at': current_scan_time}

        save_state()
        
        if valid_alerts:
            valid_alerts.sort(key=lambda x: (x['match_dt'] < current_scan_time, x['match_dt']))
            send_telegram_message("🚨 Pinna 最新凱利達標：\n\n" + "\n\n".join([x['msg'] for x in valid_alerts]))

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    load_state()
    run_scraper_once()
