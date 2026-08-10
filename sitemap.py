import os
import re
import json
import asyncio
import random
import hashlib
import tempfile
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from collections import defaultdict
from urllib.parse import unquote
from markitdown import MarkItDown



# 再帰的な辞書（ツリー構造）を作成するためのヘルパー関数
def tree():
    return defaultdict(tree)


class SitemapCrawler:
    """asyncioとPlaywrightを使用して並列クローリングを行うクラス（レートリミット＆負荷軽減対策付き）"""

    def __init__(
        self,
        start_urls,
        max_depth=2,
        base_urls=None,
        max_concurrent=5,
        request_delay=1.5,
        backoff_delay=30,
        markdown_mode=False,
        stealth_mode=False,
        html_mode=False
    ):
        # 開始するURL
        if isinstance(start_urls, str):
            self.start_urls = [start_urls]
        else:
            self.start_urls = start_urls

        self.max_depth = max_depth  # 最大の深さ
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
        self.markdown_mode = markdown_mode
        self.html_mode = html_mode
        self.stealth_mode = stealth_mode
        self.rate_limit_lock = asyncio.Lock()  # 重複して待機処理に入るのを防ぐロック
        self.rate_limit_event = (
            asyncio.Event()
        )  # 他のワーカーを一時停止させるためのイベント
        self.rate_limit_event.set()  # 初期状態は「一時停止なし（シグナルON）」

        # 停止処理用のフラグ
        self._stop_requested = False
        self.interrupted = False

        if self.markdown_mode:
            self.md = MarkItDown()

    def stop(self):
        """ワーカーを安全に停止するためのフラグ設定とキューのクリアを行う"""
        self._stop_requested = True
        self.rate_limit_event.set()  # 待機中のワーカーを解放

        # キューに残っているタスクをクリアして、完了待ちを早く終わらせる
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:
                break

    IGNORED_EXTENSIONS = (
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
        ".pdf", ".zip", ".tar", ".gz", ".rar", ".7z",
        ".mp3", ".mp4", ".wav", ".avi", ".mov", ".webm",
        ".css", ".js", ".json", ".xml", ".woff", ".woff2", ".ttf", ".eot"
    )

    def extract_links(self, html, base_url):
        # htmlからurlを抜き出す
        soup = BeautifulSoup(html, "lxml")
        extracted = set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
                continue
            full_url = urljoin(base_url, href)
            clean_url = full_url.split("#")[0]
            if not clean_url.startswith(("http://", "https://")):
                continue

            parsed = urlparse(clean_url)
            # 非HTMLリソース（画像・ファイル等）を除外
            if any(parsed.path.lower().endswith(ext) for ext in self.IGNORED_EXTENSIONS):
                continue

            # 末尾のスラッシュを統一（ルートパス以外は末尾スラッシュ除去）
            path = parsed.path
            if len(path) > 1 and path.endswith("/"):
                path = path.rstrip("/")
            normalized_url = parsed._replace(path=path).geturl()

            extracted.add(normalized_url)
        return extracted

    def build_tree_path(self, url):
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        current_node = self.sitemap_tree
        if not path_parts:
            current_node["(index)"]
        else:
            for part in path_parts:
                current_node = current_node[part]

    async def handle_rate_limit(self, triggered_url):
        """レートリミットを検知した際に、全ワーカーを一時停止させて一定時間待機する"""
        async with self.rate_limit_lock:
            if getattr(self, "_stop_requested", False):
                return

            # 既に他のワーカーが検知して一時停止中の場合は、その解除を待つだけにする
            if not self.rate_limit_event.is_set():
                print(
                    f"[一時停止] 既に他のワーカーがアクセス制限を検知して待機中です。解除を待機します... ({unquote(triggered_url)})"
                )
                return

            self.rate_limit_event.clear()  # イベントをクリアして他ワーカーの進行を一時ブロック

            print(f"\n" + "!" * 60)
            print(f"[⚠️ アクセス制限を検知]")
            print(f"対象URL: {unquote(triggered_url)}")
            print(
                f"安全のため全並列ワーカーを一時停止し、{self.backoff_delay}秒間待機します..."
            )
            print("!" * 60 + "\n")

            # 指定時間スリープ（停止要求が来たらすぐに抜けられるように細かく待機）
            for _ in range(self.backoff_delay):
                if getattr(self, "_stop_requested", False):
                    break
                await asyncio.sleep(1)

            if not getattr(self, "_stop_requested", False):
                print(f"\n" + "=" * 60)
                print(f"[再開] 待機時間が終了しました。クローリングを再開します。")
                print("=" * 60 + "\n")

            self.rate_limit_event.set()  # イベントをセットして全ワーカーを再稼働

    async def save_markdown(self, url, html_content):
        def _convert_and_save():
            BASE_DIR = os.path.dirname(os.path.abspath(__file__))
            md_dir = os.path.join(BASE_DIR, "markdown")
            if not os.path.exists(md_dir):
                os.makedirs(md_dir, exist_ok=True)
            
            parsed = urlparse(url)
            path = parsed.path.strip("/")
            safe_name = path.replace("/", "_")[:50]
            if not safe_name:
                safe_name = "index"
            hashed = hashlib.md5(url.encode('utf-8')).hexdigest()[:8]
            filename = f"{safe_name}_{hashed}.md"
            md_path = os.path.join(md_dir, filename)

            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".html", delete=False) as tmp:
                    tmp.write(html_content)
                    tmp_path = tmp.name
                
                result = self.md.convert(tmp_path)
                
                with open(md_path, "w", encoding="utf-8") as f:
                    f.write(result.text_content)
                
                os.remove(tmp_path)
                print(f"[Markdown保存] {md_path}")
            except Exception as e:
                print(f"[Markdown保存エラー] {url}: {e}")
                if 'tmp_path' in locals() and os.path.exists(tmp_path):
                    os.remove(tmp_path)

        await asyncio.to_thread(_convert_and_save)
    
    async def save_html(self, url, html_content):
        def save():
            BASE_DIR = os.path.dirname(os.path.abspath(__file__))
            html_dir = os.path.join(BASE_DIR, "html")
            if not os.path.exists(html_dir):
                os.makedirs(html_dir, exist_ok=True)
            parsed = urlparse(url)
            path = parsed.path.strip("/")
            safe_name = re.sub(r'[\\/*?:"<>|]', '_', path)[:50]
            if not safe_name:
                safe_name = "index"
            hashed = hashlib.md5(url.encode('utf-8')).hexdigest()[:8]
            filename = f"{safe_name}_{hashed}.html"
            html_path = os.path.join(html_dir, filename)
            try:
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(html_content)
                    print(f"[HTML保存] {html_path}")
            except Exception as e:
                print(f"[HTML保存エラー] {url}: {e}")
                
        await asyncio.to_thread(save)

    async def worker(self, session_or_context):
        """並列実行されるワーカー"""
        while not getattr(self, "_stop_requested", False):
            try:
                # レートリミット一時停止中の場合は、イベントがセット（再開）されるまでここで待機
                await self.rate_limit_event.wait()
                if getattr(self, "_stop_requested", False):
                    break

                # タイムアウト付きでキューから取得
                try:
                    current_url, depth = await asyncio.wait_for(
                        self.queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    if self.active_tasks == 0 and self.queue.empty():
                        break
                    continue
                except asyncio.CancelledError:
                    break

                self.active_tasks += 1
                try:
                    # 現在のURLはすでに訪れている、もしくは深さが指定した場合より深い場合はパスする
                    if current_url in self.visited or depth > self.max_depth:
                        continue

                    if depth == self.max_depth:
                        self.visited.add(current_url)
                        self.build_tree_path(unquote(current_url))
                        continue

                    if depth < self.max_depth:
                        remaining = self.queue.qsize()
                        print(
                            f"取得中 [残り: {remaining:3} / 並列: {self.active_tasks:2} / 重複スキップ: {self.duplicate_count:3}]: {unquote(current_url)} (深さ: {depth})"
                        )

                        # ページ取得
                        if self.stealth_mode:
                            page = None
                        else:
                            page = await session_or_context.new_page()
                            if not self.markdown_mode:
                                await page.route(
                                    "**/*",
                                    lambda route: route.continue_() if route.request.resource_type in [
                                        "document", "script"] else route.abort()
                                )
                        try:
                            success = False
                            retry_count = 0
                            max_retries = 3

                            while (
                                not success
                                and retry_count < max_retries
                                and not getattr(self, "_stop_requested", False)
                            ):
                                # 処理開始直前にも一時停止中ではないか確認
                                await self.rate_limit_event.wait()

                                try:
                                    if self.stealth_mode:
                                        response = await session_or_context.fetch(
                                            current_url,
                                            timeout=20000,
                                            disable_resources=not self.markdown_mode,
                                            solve_cloudflare=True,
                                            network_idle=True
                                        )
                                        await asyncio.sleep(1)  # JS実行待ち
                                        if response is None:
                                            raise Exception("No response received from session.fetch")
                                        status = response.status
                                        html_content = str(response.html_content)
                                    else:
                                        response = await page.goto(
                                            current_url,
                                            wait_until="domcontentloaded",
                                            timeout=20000,
                                        )
                                        await asyncio.sleep(1)  # JS実行待ち
                                        if response is None:
                                            raise Exception("No response received from page.goto")
                                        status = response.status
                                        html_content = await page.content()

                                    # レートリミット検知の判定
                                    is_rate_limited = status == 429

                                    if is_rate_limited:
                                        retry_count += 1
                                        print(
                                            f"[警告] レートリミット検知 ({retry_count}/{max_retries}回目試行): {unquote(current_url)}"
                                        )
                                        # 全ワーカーを止めて待機を実行する
                                        await self.handle_rate_limit(current_url)
                                        continue  # リトライループの先頭に戻って再試行

                                    # 正常にページが取得できたら成功とする
                                    success = True
                                    self.visited.add(current_url)
                                    self.build_tree_path(unquote(current_url))

                                    if self.markdown_mode:
                                        await self.save_markdown(current_url, html_content)
                                    
                                    if self.html_mode:
                                        await self.save_html(current_url, html_content)

                                    # ページ内のURLを取得
                                    links = self.extract_links(
                                        html_content, current_url
                                    )
                                    for link in links:
                                        matches_base = (not self.base_urls) or any(
                                            link.startswith(b)
                                            for b in self.base_urls
                                        )

                                        # ベースと一致する場合
                                        if matches_base:
                                            if link not in self.seen:
                                                self.seen.add(link)
                                                await self.queue.put(
                                                    (link, depth + 1)
                                                )
                                            else:
                                                self.duplicate_count += 1

                                except asyncio.CancelledError:
                                    raise
                                except KeyboardInterrupt:
                                    raise
                                except Exception as e:
                                    # タイムアウトやその他のネットワークエラーに対する再試行
                                    retry_count += 1
                                    if retry_count >= max_retries:
                                        raise e  # 最大回数失敗した場合は大元のexceptに投げる
                                    print(
                                        f"[一時エラー] 接続に失敗しました。再試行します ({retry_count}/{max_retries}): {e}"
                                    )
                                    # 簡易的なバックオフウェイト
                                    await asyncio.sleep(2 * retry_count)

                        except asyncio.CancelledError:
                            raise
                        except KeyboardInterrupt:
                            raise
                        except Exception as e:
                            print(f"エラー発生 ({current_url}): {e}")
                            self.visited.add(current_url)
                        finally:
                            if page:
                                await page.close()

                            # 負荷軽減のためのウェイト
                            if not getattr(self, "_stop_requested", False):
                                jitter_delay = random.uniform(
                                    self.request_delay * 0.7,
                                    self.request_delay * 1.3,
                                )
                                await asyncio.sleep(jitter_delay)

                finally:
                    self.active_tasks -= 1
                    # tryブロックに入った場合は必ず task_done() を呼び、キューのデッドロックを防ぐ
                    self.queue.task_done()

            except KeyboardInterrupt:
                self.stop()
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"ワーカー内でエラーが発生しました: {e}")

    async def crawl(self):
        print(
            f"[{self.domain}] のサイトマップを作成中...(並列数: {self.max_concurrent}, 最大深さ: {self.max_depth})"
        )
        print(
            f"遅延：{self.request_delay}, レートリミット待機時間: {self.backoff_delay}"
        )
        if self.stealth_mode:
            print("[Scrapling] 高度なステルス機能を有効にしてクローリングを実行します。")

        # 初期URLをキューに追加
        for url in self.start_urls:
            self.seen.add(url)
            await self.queue.put((url, 0))

        if self.stealth_mode:
            from scrapling.fetchers import AsyncStealthySession
            async with AsyncStealthySession(headless=True) as session:
                workers = [
                    asyncio.create_task(self.worker(session))
                    for _ in range(self.max_concurrent)
                ]

                try:
                    join_task = asyncio.create_task(self.queue.join())
                    while not join_task.done():
                        await asyncio.wait([join_task], timeout=1.0)
                        if getattr(self, "_stop_requested", False):
                            break

                except KeyboardInterrupt:
                    print("\n[中断検知] クローリングを強制停止しています。少々お待ちください...")
                    self.interrupted = True
                    self.stop()
                finally:
                    self.stop()
                    for w in workers:
                        if not w.done():
                            w.cancel()
        else:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )

                # 指定された並列数分のワーカーを起動
                workers = [
                    asyncio.create_task(self.worker(context))
                    for _ in range(self.max_concurrent)
                ]

                try:
                    # 全てのタスクが完了するのを待つ
                    # KeyboardInterruptを確実に捕捉できるよう、タイムアウトを挟んで待機
                    join_task = asyncio.create_task(self.queue.join())
                    while not join_task.done():
                        await asyncio.wait([join_task], timeout=1.0)
                        if getattr(self, "_stop_requested", False):
                            break

                except KeyboardInterrupt:
                    print(
                        "\n[中断検知] クローリングを強制停止しています。少々お待ちください..."
                    )
                    self.interrupted = True
                    self.stop()
                finally:
                    # ワーカーを停止
                    self.stop()
                    for w in workers:
                        if not w.done():
                            w.cancel()

                    try:
                        await browser.close()
                    except Exception:
                        pass

        return self.sitemap_tree


def get_tree_string(t, indent=""):
    lines = []
    keys = list(t.keys())
    for i, key in enumerate(keys):
        is_last = i == len(keys) - 1
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
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    savedir = os.path.join(BASE_DIR, "save")
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
                f"クロール完了: 合計 {visited_count} ページを処理しました。（重複スキップ: {duplicate_count}）\n"
            )
        print(f"\n[保存完了] ファイル名: {filename}")
    except Exception as e:
        print(f"\n[保存失敗] エラー: {e}")


async def main():
    # ==========================================
    # 設定
    # ==========================================
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(BASE_DIR, "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        configs = json.load(f)

    for config in configs:
        try:
            TARGET_URLS = config["target_urls"]
            BASE_URLS = config["base_urls"]
            MAX_DEPTH = config["max_depth"]
            MAX_CONCURRENT = config[
                "max_concurrent"
            ]  # 並列数（増やすと速くなりますが負荷も上がる）

            # オプション設定: なければデフォルト値を適用
            # リクエスト間ディレイ (秒)
            REQUEST_DELAY = config.get("request_delay", 1.5)
            # レートリミット検知時の待機時間 (秒)
            BACKOFF_DELAY = config.get("backoff_delay", 30)
            # マークダウン保存モード
            MARKDOWN_MODE = config.get("markdown_mode", False)
            # HTML保存モード
            HTML_MODE = config.get("html_mode", False)
            # ステルスモード
            STEALTH_MODE = config.get("stealth_mode", False)

        except KeyError as e:
            print(e)
            print("指定されていないキーがあります。config.jsonを確認してください。")
            print("\n" + "=" * 50)
            continue

        crawler = SitemapCrawler(
            TARGET_URLS,
            max_depth=MAX_DEPTH,
            base_urls=BASE_URLS,
            max_concurrent=MAX_CONCURRENT,
            request_delay=REQUEST_DELAY,
            backoff_delay=BACKOFF_DELAY,
            markdown_mode=MARKDOWN_MODE,
            html_mode=HTML_MODE,
            stealth_mode=STEALTH_MODE,
        )

        # クローリング開始
        result_tree = await crawler.crawl()

        # 中断された場合でも、その時点のデータを使って保存まで行う
        print("\n" + "=" * 50)
        print(
            f"Site Map Tree: {crawler.domain}"
            + (" (途中経過)" if crawler.interrupted else "")
        )
        print("=" * 50)
        print(f"{crawler.domain}/")
        print_tree(result_tree)
        print("=" * 50)

        status_msg = "中断" if crawler.interrupted else "クロール完了"
        print(
            f"{status_msg}: 合計 {len(crawler.visited)} ページを処理しました。（重複スキップ: {crawler.duplicate_count}）"
        )

        save_to_file(
            crawler.domain, result_tree, len(crawler.visited), crawler.duplicate_count
        )

        # ユーザーによる中断操作が行われた場合は、他の設定を処理せずに全体の処理を終了する
        if crawler.interrupted:
            print(
                "\nユーザー操作により中断されたため、残りの処理をスキップして終了します。"
            )
            break


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # メイン処理の外側で捕捉された場合の念のためのフォールバック
        pass
    except Exception as e:
        print(f"エラーが発生しました: {e}")
