import os
import time
import urllib.request
import datetime
import yfinance as yf
import pandas as pd
import requests
import feedparser
from bs4 import BeautifulSoup

def get_jpx_tickers():
    """JPXのサイトから最新の上場銘柄一覧（Excel）を取得し、ティッカーのリストを返す"""
    print("JPXの銘柄一覧を取得中...", flush=True)
    url = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        # タイムアウトを設定し、フリーズを防止
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        xls_url = None
        for a in soup.find_all('a'):
            href = a.get('href')
            if href and href.endswith('.xls'):
                xls_url = "https://www.jpx.co.jp" + href
                break
        
        if not xls_url:
            raise Exception("Excelファイルのリンクが見つかりませんでした。")
        
        print(f"銘柄一覧Excelをダウンロード中: {xls_url}")
        xls_resp = requests.get(xls_url, headers=headers, timeout=15)
        xls_resp.raise_for_status()
        
        with open("jpx_data.xls", "wb") as f:
            f.write(xls_resp.content)
            
        df = pd.read_excel("jpx_data.xls")
        # プライム市場のみを抽出
        target_markets = ['プライム（内国株式）']
        df = df[df['市場・商品区分'].isin(target_markets)]
        
        # 銘柄名も保持しておく（ニュース検索用）
        tickers = []
        for index, row in df.iterrows():
            code = str(row['コード'])
            name = row['銘柄名']
            tickers.append({"ticker": f"{code}.T", "name": name, "code": code})
        
        return tickers
    except Exception as e:
        print(f"銘柄一覧の取得に失敗しました: {e}")
        return []

def fetch_and_filter_stocks(tickers_info):
    """yfinanceを用いて株価データを取得し、条件に合う銘柄を絞り込む"""
    print(f"全 {len(tickers_info)} 銘柄のデータを分割して取得・解析します...")
    
    candidates = []
    chunk_size = 200  # サーバー負荷とフリーズを防ぐため200件ずつ処理
    
    for i in range(0, len(tickers_info), chunk_size):
        chunk_info = tickers_info[i:i + chunk_size]
        chunk_symbols = [t["ticker"] for t in chunk_info]
        
        print(f"処理中... {i+1} 〜 {min(i+chunk_size, len(tickers_info))} / {len(tickers_info)} 銘柄")
        
        # yfinanceでダウンロード
        data = yf.download(chunk_symbols, period="1mo", group_by="ticker", threads=True, progress=False)
        
        for t_info in chunk_info:
            symbol = t_info["ticker"]
            try:
                # MultiIndexカラムのため、銘柄ごとのデータを取得
                if len(chunk_symbols) == 1:
                    df = data
                else:
                    df = data[symbol]
                    
                if df.empty or len(df) < 20:
                    continue
                    
                # 欠損値の削除
                df = df.dropna()
                if len(df) < 20:
                    continue
                    
                # 直近の終値と出来高の平均
                current_price = df['Close'].iloc[-1]
                avg_volume = df['Volume'].mean()
                
                # デイトレード向けの事前除外: 
                # 1. 価格が低すぎる（100円未満）または高すぎる（7000円超）銘柄を除外
                # 2. 平均出来高が少なすぎる（10万株未満）銘柄を除外
                if current_price < 100 or current_price > 7000 or avg_volume < 100000:
                    continue
                    
                # 1ヶ月前（約20営業日前）、2週間前（約10営業日前）、1週間前（約5営業日前）の価格
                price_1mo_ago = df['Close'].iloc[0]
                price_2wk_ago = df['Close'].iloc[-10]
                price_1wk_ago = df['Close'].iloc[-5]
                
                # 騰落率の計算
                return_1mo = (current_price - price_1mo_ago) / price_1mo_ago
                return_2wk = (current_price - price_2wk_ago) / price_2wk_ago
                return_1wk = (current_price - price_1wk_ago) / price_1wk_ago
                
                # 条件判定:
                # 1. 1ヶ月でプラス
                # 2. 2週間でプラス
                # 3. 1週間でプラス
                # 4. 1週間の上昇率が、1ヶ月の平均週間ペース（1ヶ月の上昇率 ÷ 4）を上回っている（勢いが加速している）
                if return_1mo > 0 and return_2wk > 0 and return_1wk > 0 and return_1wk > (return_1mo / 4.0):
                    candidates.append({
                        "code": t_info["code"],
                        "name": t_info["name"],
                        "price": current_price,
                        "return_1wk": return_1wk * 100,
                        "return_1mo": return_1mo * 100,
                        "volume": avg_volume
                    })
            except Exception as e:
                # データが存在しない銘柄などはスキップ
                continue
                
        # サーバー負荷軽減のため1秒待機
        time.sleep(1)
            
    # 1週間の上昇率が高い順にソートし、上位10銘柄を取得
    candidates.sort(key=lambda x: x["return_1wk"], reverse=True)
    return candidates[:10]

def get_recent_news(company_name):
    """GoogleニュースのRSSから直近のニュースを取得し、キーワード判定を行う"""
    # 会社名で検索
    query = urllib.parse.quote(company_name)
    rss_url = f"https://news.google.com/rss/search?q={query}&hl=ja&gl=JP&ceid=JP:ja"
    
    positive_keywords = ["上方修正", "黒字", "増配", "提携", "好決算", "自社株買い"]
    ir_keywords = ["IR", "適時開示", "決算", "発表"]
    
    try:
        feed = feedparser.parse(rss_url)
        yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
        
        relevant_news = []
        
        for entry in feed.entries:
            # ニュースの日時が前日以降かチェック
            try:
                published_dt = datetime.datetime(*entry.published_parsed[:6])
                if published_dt < yesterday:
                    continue
            except:
                pass
                
            title = entry.title
            
            # キーワードが含まれているか判定
            has_positive = any(kw in title for kw in positive_keywords)
            has_ir = any(kw in title for kw in ir_keywords)
            
            if has_positive or has_ir:
                relevant_news.append({
                    "title": title,
                    "link": entry.link,
                    "is_positive": has_positive
                })
                # 最大3件まで
                if len(relevant_news) >= 3:
                    break
                    
        return relevant_news
    except Exception as e:
        print(f"ニュース取得エラー ({company_name}): {e}")
        return []

def send_slack_notification(webhook_url, stocks):
    """Slackへ通知を送信する"""
    if not stocks:
        message = "本日のデイトレード推奨銘柄（条件合致）はありませんでした。"
    else:
        message = "*【本日のデイトレード注目銘柄リスト】*\n"
        message += "直近1週間の上昇の勢いが強く、一定の出来高がある銘柄を抽出しました。\n\n"
        
        for i, stock in enumerate(stocks, 1):
            message += f"*{i}. {stock['name']} ({stock['code']})* - 現在値: {stock['price']:.1f}円\n"
            message += f"  📈 1週間上昇率: +{stock['return_1wk']:.2f}% (1ヶ月: +{stock['return_1mo']:.2f}%)\n"
            message += f"  📊 平均出来高: {int(stock['volume']):,} 株\n"
            
            # ニュースの付与
            if stock['news']:
                message += "  📰 *関連ニュース/IR*\n"
                for n in stock['news']:
                    tag = "[好材料]" if n['is_positive'] else "[開示/IR]"
                    message += f"    ・ {tag} <{n['link']}|{n['title']}>\n"
            message += "\n"
            
    payload = {
        "text": message
    }
    
    response = requests.post(webhook_url, json=payload)
    if response.status_code == 200:
        print("Slackへの通知が成功しました。")
    else:
        print(f"Slackへの通知に失敗しました: {response.text}")

def main():
    print(f"[{datetime.datetime.now()}] エージェントの実行を開始します。", flush=True)
    
    # 1. 銘柄一覧の取得
    tickers_info = get_jpx_tickers()
    if not tickers_info:
        print("銘柄情報の取得に失敗したため終了します。")
        return
        
    # 2. 株価データの取得と絞り込み
    selected_stocks = fetch_and_filter_stocks(tickers_info)
    print(f"{len(selected_stocks)}件の銘柄が選出されました。")
    
    # 3. ニュース情報の取得
    for stock in selected_stocks:
        news = get_recent_news(stock["name"])
        stock["news"] = news
        
    # 4. 通知の送信
    slack_webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if slack_webhook_url:
        send_slack_notification(slack_webhook_url, selected_stocks)
    else:
        print("警告: 環境変数 SLACK_WEBHOOK_URL が設定されていません。コンソールに出力します。")
        for stock in selected_stocks:
            print(f"{stock['name']} ({stock['code']}) - {stock['return_1wk']:.2f}%")
            for n in stock['news']:
                print(f"  -> {n['title']}")
                
    print("実行が完了しました。")

if __name__ == "__main__":
    main()
