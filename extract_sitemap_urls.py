import os
import glob
import re
import csv
from urllib.parse import unquote

os.chdir(os.path.dirname(os.path.abspath(__file__)))

def parse_sitemap_md(file_path):
    """MarkdownファイルからURLを抽出する"""
    urls = []
    domain = ""
    
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # ドメイン名の抽出 (ヘッダーから)
    domain_match = re.search(r'# Site Map Tree: ([\w\.-]+)', content)
    if not domain_match:
        return []
    domain = domain_match.group(1)
    base_url = f"https://{domain}"
    urls.append(base_url + "/")

    # コードブロック内のツリー部分を抽出
    tree_match = re.search(r'```\n(.*?)\n```', content, re.DOTALL)
    if not tree_match:
        return []
    
    tree_lines = tree_match.group(1).split('\n')
    
    # パスを保持するためのスタック (階層管理用)
    path_stack = [] 
    
    for line in tree_lines:
        # ドメイン名のみの行（ルート）はスキップ
        if line.strip() == f"{domain}/" or not line.strip():
            continue
            
        # インデントの深さを計算 (4文字で1レベル)
        # プレフィックス (├── , └── , │   ,     ) を解析
        prefix_match = re.match(r'^([│\s\s\s\s]*)([├└]──\s)(.+)/', line)
        if prefix_match:
            indent_part = prefix_match.group(1)
            # インデントレベル = (インデント部分の長さ / 4) + 1
            level = (len(indent_part) // 4) + 1
            node_name = prefix_match.group(3)
            
            # 現在のレベルに合わせてスタックを調整
            path_stack = path_stack[:level-1]
            path_stack.append(node_name)
            
            # URLの組み立て
            full_path = "/".join(path_stack)
            decoded_text = unquote(f"{base_url}/{full_path}")
            urls.append(decoded_text)
        
    urls.sort()
    return urls

def main():
    # sitemap_*.md ファイルをすべて取得
    md_files = glob.glob("./save/sitemap_*.md")
    
    if not md_files:
        print("./save/sitemap_*.md ファイルが見つかりませんでした。")
        return

    for md_file in md_files:
        print(f"解析中: {md_file}...")
        urls = parse_sitemap_md(md_file)
        
        if not urls:
            print(f"  警告: {md_file} からURLを抽出できませんでした。")
            continue
            
        csv_file = md_file.replace(".md", ".csv")
        
        try:
            with open(csv_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                for url in urls:
                    writer.writerow([url])
            print(f"  [保存完了] -> {csv_file} ({len(urls)} 件)")
        except Exception as e:
            print(f"  [エラー] 書き込み失敗: {e}")

if __name__ == "__main__":
    main()
