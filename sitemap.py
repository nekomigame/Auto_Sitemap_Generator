import os
import json
import asyncio
import random
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from collections import defaultdict
from urllib.parse import unquote

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# 再帰的な辞書（ツリー構造）を作成するためのヘルパー関数
def tree():
    return defaultdict(tree)


class SitemapCrawler:
    """asyncioとPlaywrightを使用して並列クローリングを行うクラス（レートリミット＆負荷軽減対策付き）"""

    def __init__(self, start_urls, max_depth=2, base_urls=None, max_concurrent=5, request_delay=1.5, backoff_delay=30):
        # 開始するURL
        if isinstance(start_urls, str):
            self.start_urls = [start_urls]
        else:
            self.start_urls = start_urls

        self.max_depth = max_depth
        self.domain = urlparse(self.start_urls[0]).netloc

        if not base_urls:
            self.base_urls = []
        elif isinstance(base_urls, str):
            self.base_urls = [base_urls]
        else:
            self.base_urls = base_urls

        self.max_concurrent = max_concurrent
        self.visited = set()
        self.seen = set()  # すでに発見（キューイング）したURL
        self.queue = asyncio.Queue()
        self.sitemap_tree = tree()
        self.active_tasks = 0
        self.duplicate_count = 0  # 重複・既知によりスキップされたURLの数

        # 負荷軽減・レートリミット用の変数
        self.request_delay = request_delay  # リクエスト間のデフォルト遅延時間（秒）
        self.backoff_delay = backoff_delay  # レートリミット検知時の待機時間（秒）
        self.rate_limit_lock = asyncio.Lock()  # 重複して待機処理に入るのを防ぐロック
        self.rate_limit_event = asyncio.Event()  # 他のワーカーを一時停止させるためのイベント
        self.rate_limit_event.set()  # 初期状態は「一時停止なし（シグナルON）」

    def extract_links(self, html, base_url):
        soup = BeautifulSoup(html, 'html.parser')
        extracted = set()
        for a in soup.find_all('a', href=True):
            full_url = urljoin(base_url, a['href'])
            clean_url = full_url.split('#')[0].split('?')[0]
            if clean_url.startswith('http'):
                extracted.add(clean_url)
        return extracted

    def build_tree_path(self, url):
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split('/') if p]
        current_node = self.sitemap_tree
        for part in path_parts:
            current_node = current_node[part]

    async def handle_rate_limit(self, triggered_url):
        """レートリミットを検知した際に、全ワーカーを一時停止させて一定時間待機する"""
        async with self.rate_limit_lock:
            # 既に他のワーカーが検知して一時停止中の場合は、その解除を待つだけにする
            if not self.rate_limit_event.is_set():
                print(
                    f"[一時停止] 既に他のワーカーがアクセス制限を検知して待機中です。解除を待機します... ({unquote(triggered_url)})")
                return

            self.rate_limit_event.clear()  # イベントをクリアして他ワーカーの進行を一時ブロック

            print(f"\n" + "!"*60)
            print(f"[⚠️ アクセス制限を検知]")
            print(f"対象URL: {unquote(triggered_url)}")
            print(f"安全のため全並列ワーカーを一時停止し、{self.backoff_delay}秒間待機します...")
            print("!"*60 + "\n")

            # 指定時間スリープ
            await asyncio.sleep(self.backoff_delay)

            print(f"\n" + "="*60)
            print(f"[再開] 待機時間が終了しました。クローリングを再開します。")
            print("="*60 + "\n")
            self.rate_limit_event.set()  # イベントをセットして全ワーカーを再稼働

    async def worker(self, browser_context):
        """並列実行されるワーカー"""
        while True:
            try:
                # レートリミット一時停止中の場合は、イベントがセット（再開）されるまでここで待機
                await self.rate_limit_event.wait()

                # タイムアウト付きでキューから取得（キューが空でアクティブなタスクがなければ終了）
                try:
                    current_url, depth = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if self.active_tasks == 0:
                        break
                    continue

                # 現在のURLはすでに訪れている、もしくは深さが指定した場合より深い場合はパスする
                if current_url in self.visited or depth > self.max_depth:
                    self.queue.task_done()
                    continue

                self.visited.add(current_url)
                self.build_tree_path(unquote(current_url))

                if depth < self.max_depth:
                    self.active_tasks += 1
                    remaining = self.queue.qsize()
                    print(
                        f"取得中 [残り: {remaining:3} / 並列: {self.active_tasks:2} / 重複スキップ: {self.duplicate_count:3}]: {unquote(current_url)} (深さ: {depth})")

                    # ページ取得
                    page = await browser_context.new_page()
                    try:
                        success = False
                        retry_count = 0
                        max_retries = 3

                        while not success and retry_count < max_retries:
                            # 処理開始直前にも一時停止中ではないか確認
                            await self.rate_limit_event.wait()

                            try:
                                response = await page.goto(current_url, wait_until="domcontentloaded", timeout=20000)
                                await asyncio.sleep(1)  # JS実行待ち

                                # ステータスコードの確認
                                status = response.status if response else None
                                html_content = await page.content()

                                # レートリミット検知の判定
                                is_rate_limited = False
                                if status == 429:
                                    is_rate_limited = True
                                else:
                                    # HTMLテキスト中に特定のアクセス制限エラーキーワードが含まれていないかチェック
                                    lower_html = html_content.lower()
                                    limit_keywords = [
                                        "too many requests",
                                        "too many access",
                                        "rate limit",
                                        "アクセス制限",
                                        "アクセスが集中しています",
                                        "一時的にアクセスを制限"
                                    ]
                                    if any(kw in lower_html for kw in limit_keywords):
                                        is_rate_limited = True

                                if is_rate_limited:
                                    retry_count += 1
                                    print(
                                        f"[警告] レートリミット検知 ({retry_count}/{max_retries}回目試行): {unquote(current_url)}")
                                    # 全ワーカーを止めて待機を実行する
                                    await self.handle_rate_limit(current_url)
                                    continue  # リトライループの先頭に戻って再試行

                                # 正常にページが取得できたら成功とする
                                success = True

                                # ページ内のURLを取得
                                links = self.extract_links(
                                    html_content, current_url)
                                for link in links:
                                    matches_base = (not self.base_urls) or any(
                                        link.startswith(b) for b in self.base_urls)

                                    # ベースと一致する場合
                                    if matches_base:
                                        if link not in self.seen:
                                            self.seen.add(link)
                                            await self.queue.put((link, depth + 1))
                                        else:
                                            self.duplicate_count += 1

                            except Exception as e:
                                # タイムアウトやその他のネットワークエラーに対する再試行
                                retry_count += 1
                                if retry_count >= max_retries:
                                    raise e  # 最大回数失敗した場合は大元のexceptに投げる
                                print(
                                    f"[一時エラー] 接続に失敗しました。再試行します ({retry_count}/{max_retries}): {e}")
                                # 簡易的なバックオフウェイト
                                await asyncio.sleep(2 * retry_count)

                    except Exception as e:
                        print(f"エラー発生 ({current_url}): {e}")
                    finally:
                        await page.close()
                        self.active_tasks -= 1

                        # 負荷軽減のためのウェイト（一定秒数ではなく、指定秒数を基準に前後30%のランダムな揺らぎ（Jitter）を持たせる）
                        jitter_delay = random.uniform(
                            self.request_delay * 0.7, self.request_delay * 1.3)
                        await asyncio.sleep(jitter_delay)

                self.queue.task_done()
            except Exception as e:
                print(f"ワーカー内でエラーが発生しました: {e}")
                self.queue.task_done()

    async def crawl(self):
        print(
            f"[{self.domain}] のサイトマップを作成中...(並列数: {self.max_concurrent}, 最大深さ: {self.max_depth})")

        # 初期URLをキューに追加
        for url in self.start_urls:
            self.seen.add(url)
            await self.queue.put((url, 0))

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )

            # 指定された並列数分のワーカーを起動
            workers = [asyncio.create_task(self.worker(
                context)) for _ in range(self.max_concurrent)]

            # 全てのタスクが完了するのを待つ
            await self.queue.join()

            # ワーカーを停止
            for w in workers:
                w.cancel()

            await browser.close()

        return self.sitemap_tree


def get_tree_string(t, indent=""):
    lines = []
    keys = list(t.keys())
    for i, key in enumerate(keys):
        is_last = (i == len(keys) - 1)
        prefix = "└── " if is_last else "├── "
        lines.append(f"{indent}{prefix}{key}/")
        child_indent = indent + ("    " if is_last else "│   ")
        lines.extend(get_tree_string(t[key], child_indent))
    return lines


def print_tree(t, indent=""):
    lines = get_tree_string(t, indent)
    for line in lines:
        print(line)


def save_to_file(domain, result_tree, visited_count, duplicate_count):
    filename = f"sitemap_{domain.replace('.', '_')}.md"
    savedir = "save/"
    if not os.path.exists(savedir):
        os.makedirs(savedir)
    filename = os.path.join(savedir, filename)
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"# Site Map Tree: {domain}\n\n")
            f.write("```\n")
            f.write(f"{domain}/\n")
            lines = get_tree_string(result_tree)
            f.write("\n".join(lines))
            f.write("\n```\n\n")
            f.write(
                f"クロール完了: 合計 {visited_count} ページを処理しました。（重複スキップ: {duplicate_count}）\n")
        print(f"\n[保存完了] ファイル名: {filename}")
    except Exception as e:
        print(f"\n[保存失敗] エラー: {e}")


async def main():
    # ==========================================
    # 設定
    # ==========================================
    with open("config.json", "r") as f:
        configs = json.load(f)

    for config in configs:
        try:
            TARGET_URLS = config["target_urls"]
            BASE_URLS = config["base_urls"]
            MAX_DEPTH = config["max_depth"]
            MAX_CONCURRENT = config["max_concurrent"]  # 並列数（増やすと速くなりますが負荷も上がる）

            # オプション設定: なければデフォルト値を適用
            # リクエスト間ディレイ (秒)
            REQUEST_DELAY = config.get("request_delay", 1.5)
            # レートリミット検知時の待機時間 (秒)
            BACKOFF_DELAY = config.get("backoff_delay", 30)

        except KeyError as e:
            print(e)
            print("指定されていないキーがあります。config.jsonを確認してください。")
            print("\n" + "="*50)
            continue

        crawler = SitemapCrawler(
            TARGET_URLS,
            max_depth=MAX_DEPTH,
            base_urls=BASE_URLS,
            max_concurrent=MAX_CONCURRENT,
            request_delay=REQUEST_DELAY,
            backoff_delay=BACKOFF_DELAY
        )
        result_tree = await crawler.crawl()

        print("\n" + "="*50)
        print(f"Site Map Tree: {crawler.domain}")
        print("="*50)
        print(f"{crawler.domain}/")
        print_tree(result_tree)
        print("="*50)
        print(
            f"クロール完了: 合計 {len(crawler.visited)} ページを処理しました。（重複スキップ: {crawler.duplicate_count}）")

        save_to_file(crawler.domain, result_tree, len(
            crawler.visited), crawler.duplicate_count)

if __name__ == '__main__':
    asyncio.run(main())
