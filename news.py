import feedparser
from urllib.parse import quote


def get_news_summary(stock_name):
    if not stock_name:
        return "종목명 없음"

    query = quote(f"{stock_name} 주식")
    url = f"https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"

    try:
        feed = feedparser.parse(url)

        if not feed.entries:
            return "관련 뉴스 없음"

        titles = [entry.title for entry in feed.entries[:2]]
        return " | ".join(titles)

    except Exception:
        return "뉴스 조회 실패"