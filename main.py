from __future__ import annotations

import argparse
import os
import re
import shutil
import time
import urllib.parse
from typing import Dict, List, Optional, Tuple
from dotenv import load_dotenv

import requests
from selenium import webdriver
from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.common.exceptions import TimeoutException, NoSuchFrameException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

IMG_TAG_PATTERN = r'<img[^>]*?src="([^"]+)"[^>]*?>'

# Загрузка переменных окружения из .env файла
load_dotenv()


class DocPage:
    """Класс для хранения информации о странице документации"""

    def __init__(self, url: str, title: str, level: int = 0, number: str = "") -> None:
        self.url: str = url
        self.title: str = title
        self.level: int = level
        self.number: str = number
        self.editions: List[Tuple[str, str, str]] = []  # (suffix, label, filename)

    def __str__(self) -> str:
        return f"{self.number} {'  ' * self.level}{self.title}"


def download_image(browser: WebDriver, img_element: WebElement, save_dir: str) -> Optional[str]:
    """Скачивает изображение и возвращает имя сохраненного файла"""
    src = None
    try:
        src = img_element.get_attribute('src')
        if not src:
            return None

        if src.startswith('/db/'):
            src = f"https://its.1c.ru{src}"
        elif src.startswith('/'):
            base_url = urllib.parse.urlparse(browser.current_url).netloc
            src = f"https://{base_url}{src}"
        elif not src.startswith('http'):
            current_path = urllib.parse.urlparse(browser.current_url).path
            base_path = os.path.dirname(current_path)
            src = f"https://its.1c.ru{base_path}/{src}"

        if args.verbose:
            print(f"Скачиваем изображение: {src}")

        os.makedirs(save_dir, exist_ok=True)

        url_path = urllib.parse.urlparse(src).path
        file_ext = os.path.splitext(url_path)[1] or '.png'

        existing_images = len([f for f in os.listdir(save_dir) if f.endswith(file_ext)])
        new_filename = f"image{existing_images + 1:03d}{file_ext}"
        save_path = os.path.join(save_dir, new_filename)

        cookies = {cookie['name']: cookie['value'] for cookie in browser.get_cookies()}
        headers = {'Referer': browser.current_url}

        response = requests.get(
            src,
            cookies=cookies,
            headers=headers,
            allow_redirects=True,
            timeout=15
        )

        if response.status_code == 200:
            with open(save_path, 'wb') as f:
                f.write(response.content)
            if args.verbose:
                print(f"Сохранено изображение: {save_path}")
            return new_filename
        else:
            if args.verbose:
                print(f"Ошибка при скачивании {src}: статус {response.status_code}")

    except Exception as e:
        if args.verbose:
            print(f"Ошибка при скачивании изображения {src}: {str(e)}")
    return None


def expand_tree_by_clicking(browser: WebDriver) -> None:
    """Последовательно раскрывает все свернутые узлы дерева реальными кликами"""
    if args.verbose:
        print("Запуск пошагового раскрытия дерева оглавления через клики...")

    browser.execute_script("""
        const tree = document.querySelector('.tree');
        if (tree) {
            tree.style.maxHeight = 'none';
            tree.style.overflow = 'visible';
        }
    """)

    max_iterations = 50
    for iteration in range(max_iterations):
        clicked_count = browser.execute_script("""
            const tree = document.querySelector('.tree');
            if (!tree) return 0;

            const collapsedNodes = Array.from(tree.querySelectorAll('li.collapsed'));
            let clicked = 0;

            for (const li of collapsedNodes) {
                if (li.dataset.expandAttempted === 'true') continue;

                const toggle = li.querySelector(':scope > .expand, :scope > .hitarea, :scope > .tree-toggle, :scope > span:not(:has(a))')
                            || li.querySelector('.expand, .hitarea');

                if (toggle) {
                    li.dataset.expandAttempted = 'true';
                    toggle.click();
                    clicked++;
                }
            }
            return clicked;
        """)

        if clicked_count == 0:
            if args.verbose:
                print(f"Раскрытие дерева завершено на итерации {iteration + 1}: нераскрытых узлов больше нет.")
            break

        if args.verbose:
            print(f"Итерация {iteration + 1}: выполнено кликов по узлам: {clicked_count}. Ожидание подгрузки...")

        try:
            WebDriverWait(browser, 10).until(
                lambda d: d.execute_script("return (typeof jQuery !== 'undefined') ? jQuery.active === 0 : true;")
            )
        except Exception:
            pass
        time.sleep(1.0)


def extract_doc_structure(browser: WebDriver) -> List[DocPage]:
    """Извлекает полную структуру документации из оглавления на любую глубину"""
    print("Извлекаем структуру документации...")

    tree = WebDriverWait(browser, 15).until(
        EC.presence_of_element_located((By.CLASS_NAME, "tree"))
    )

    expand_tree_by_clicking(browser)

    def clean_title(title: str) -> str:
        title = re.sub(r'\s+', ' ', title)
        title = title.replace('__', ' — ')
        return title.strip()

    js_results = browser.execute_script("""
        const tree = document.querySelector('.tree');
        if (!tree) return [];

        const allLinks = Array.from(tree.querySelectorAll('a'));
        return allLinks
            .filter(link => link.href && link.textContent.trim())
            .map(link => {
                let ulCount = 0;
                let current = link.parentElement;
                while (current && current !== tree) {
                    if (current.tagName === 'UL') {
                        ulCount++;
                    }
                    current = current.parentElement;
                }

                return {
                    url: link.href,
                    title: link.textContent.trim(),
                    ulCount: ulCount
                };
            });
    """)

    pages: List[DocPage] = []
    processed_urls = set()

    if js_results:
        valid_items = [
            item for item in js_results
            if item.get('url') and item.get('title')
            and any(keyword in item['url'] for keyword in ["content", "bookmark", "browse"])
        ]

        if valid_items:
            min_ul = min(item['ulCount'] for item in valid_items)
            max_level = max(item['ulCount'] - min_ul for item in valid_items)
            level_counters = [0] * (max_level + 2)

            for item in valid_items:
                url = item['url']
                if url in processed_urls:
                    continue

                title = clean_title(item['title'])
                level = max(0, item['ulCount'] - min_ul)

                level_counters[level] += 1
                for deeper in range(level + 1, len(level_counters)):
                    level_counters[deeper] = 0

                section_parts = [str(level_counters[l]) for l in range(level + 1) if level_counters[l] > 0]
                section_number = '.'.join(section_parts) + '.'

                page = DocPage(url=url, title=title, level=level, number=section_number)
                pages.append(page)
                processed_urls.add(url)

                if args.verbose:
                    print(f"Добавлена страница: {page}")

    if not pages:
        if args.verbose:
            print("JS-сбор не дал результатов, пробуем резервный обход через Selenium...")
        links = tree.find_elements(By.TAG_NAME, "a")
        for idx, link in enumerate(links, 1):
            try:
                url = link.get_attribute("href")
                title = clean_title(link.text)
                if url and title and url not in processed_urls and any(k in url for k in ["content", "bookmark", "browse"]):
                    pages.append(DocPage(url=url, title=title, level=0, number=f"{idx}."))
                    processed_urls.add(url)
            except Exception:
                continue

    if not pages:
        print("Внимание: не найдено ни одной страницы в дереве")
    else:
        max_found_level = max(p.level for p in pages)
        print(f"Найдено {len(pages)} страниц (максимальная глубина вложенности: {max_found_level + 1} ур.)")

    return pages


def _navigate_to_its_url(browser: WebDriver, url: str) -> None:
    """Переходит по URL ИТС с гарантированным обновлением страницы даже при смене только #hash"""
    if browser.current_url.split('#')[0] == url.split('#')[0]:
        browser.get("about:blank")
    browser.get(url)
    time.sleep(2)


def _extract_doc_id_and_suffix(url: str) -> Optional[Tuple[str, str]]:
    """Извлекает (doc_id, suffix) из URL вида ...#content:35107:hdoc или .../content/35107/zup30"""
    m = re.search(r'(?:#content:|/content/)(\d+)(?::|/)([a-zA-Z0-9_]+)', url)
    if m:
        return m.group(1), m.group(2)
    return None


def _get_db_root_url(url: str) -> str:
    """Возвращает корневой URL базы ИТС (например, https://its.1c.ru/db/staff1c)"""
    no_hash = url.split('#')[0]
    return re.sub(r'/content/.*$', '', no_hash).rstrip('/')


def _discover_article_editions(browser: WebDriver, base_page_url: str, iframe_id: str) -> Tuple[Optional[str], Optional[str], List[Tuple[str, str, str]]]:
    """
    Ищет все вкладки редакций (например, :hdoc, :zup30, :buh30) для открытой статьи
    по ссылкам, data-атрибутам и onclick-обработчикам в основном окне и внутри iframe.
    """
    editions: List[Tuple[str, str, str]] = []
    parsed = _extract_doc_id_and_suffix(base_page_url)
    if not parsed:
        return None, None, editions

    doc_id, current_suffix = parsed
    seen_suffixes = {current_suffix}
    db_root_url = _get_db_root_url(base_page_url)

    def collect_from_current_context() -> None:
        found = browser.execute_script("""
            const docId = arguments[0];
            const results = [];
            const candidates = Array.from(document.querySelectorAll('a, li, span, button, div[onclick], [data-href], [data-url], [data-edition]'));
            for (const el of candidates) {
                const attrs = [
                    el.getAttribute('href'),
                    el.getAttribute('data-href'),
                    el.getAttribute('data-url'),
                    el.getAttribute('onclick'),
                    el.getAttribute('data-edition')
                ].filter(Boolean).join(' ');

                if (attrs.includes('content:' + docId + ':') || attrs.includes('content/' + docId + '/')) {
                    const text = (el.textContent || el.getAttribute('title') || '').trim();
                    results.push({raw: attrs, text: text});
                }
            }
            return results;
        """, doc_id)

        for item in found or []:
            raw = item.get('raw', '')
            label = re.sub(r'\s+', ' ', item.get('text', '')).strip()
            for m in re.finditer(rf'(?:#content:|/content/){doc_id}(?::|/)([a-zA-Z0-9_]+)', raw):
                suffix = m.group(1)
                if suffix not in seen_suffixes:
                    seen_suffixes.add(suffix)
                    target_url = f"{db_root_url}#content:{doc_id}:{suffix}"
                    editions.append((suffix, label or suffix, target_url))

    # 1. Ждем загрузки iframe, чтобы убедиться, что SPA-оболочка и статья полностью отрисовались
    try:
        WebDriverWait(browser, 15).until(
            EC.frame_to_be_available_and_switch_to_it((By.ID, iframe_id))
        )
        WebDriverWait(browser, 15).until(
            lambda x: x.execute_script("return document.readyState") == "complete"
        )
        time.sleep(0.8)
        collect_from_current_context()
    except Exception:
        pass
    finally:
        browser.switch_to.default_content()

    # 2. Проверяем в основном окне (панель вкладок редакций над iframe)
    collect_from_current_context()

    return doc_id, current_suffix, editions


def _rewrite_edition_links_in_dir(page_dir: str, doc_id: str, suffix_to_filename: Dict[str, str]) -> None:
    """
    Заменяет во всех сохраненных HTML-файлах статьи ссылки вида
    href="/db/.../content/33620/zup30" и href="#content:33620:zup30"
    на локальные имена файлов (page.html, page_zup30.html, page_buh30.html).
    """
    if not doc_id or not suffix_to_filename:
        return

    link_pattern = re.compile(
        rf'href=["\'][^"\']*?(?:#content:|/content/){doc_id}(?::|/)([a-zA-Z0-9_]+)(?:[:/][^"\']*)?["\']',
        re.IGNORECASE
    )

    for filename in os.listdir(page_dir):
        if not filename.endswith('.html'):
            continue
        file_path = os.path.join(page_dir, filename)
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        def replace_match(match: re.Match) -> str:
            suffix = match.group(1)
            if suffix in suffix_to_filename:
                return f'href="{suffix_to_filename[suffix]}"'
            return match.group(0)

        new_content = link_pattern.sub(replace_match, content)
        if new_content != content:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)


def _generate_html_styles() -> str:
    """Возвращает CSS стили для оглавления"""
    return """
        body { 
            font-family: Arial, sans-serif; 
            line-height: 1.6;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }
        body.show-metadata .metadata {
            display: block !important;
            position: static !important;
            margin-left: 20px !important;
            box-shadow: none !important;
            border: none !important;
            background: transparent !important;
            padding: 2px 0 !important;
            font-size: 0.8em !important;
            color: #777 !important;
        }
        body.show-metadata .toc-entry:hover .metadata {
            background: transparent !important;
        }
        .controls {
            margin: 15px 0;
            padding: 10px;
            background: #f8f8f8;
            border-radius: 5px;
            display: flex;
            flex-wrap: wrap;
            gap: 5px;
        }
        .toc { 
            margin-left: 20px; 
            max-width: 100%;
            overflow-x: hidden;
        }
        .toc-entry { 
            margin: 8px 0;
            position: relative;
            padding: 3px 5px;
            overflow: hidden;
            border-radius: 4px;
        }
        .entry-row {
            display: flex;
            align-items: center;
            flex-wrap: wrap;
            gap: 8px;
        }
        .editions-badges {
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }
        .edition-badge {
            font-size: 0.8em;
            padding: 2px 7px;
            background-color: #e8f0fe;
            color: #1967d2;
            border: 1px solid #d2e3fc;
            border-radius: 12px;
            text-decoration: none;
            transition: background-color 0.2s;
        }
        .edition-badge:hover {
            background-color: #d2e3fc;
            text-decoration: none;
        }
        .actions {
            display: flex;
            align-items: center;
            gap: 4px;
            opacity: 0.2;
            transition: opacity 0.2s;
        }
        .toc-entry:hover .actions {
            opacity: 1;
        }
        .original-link {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 24px;
            height: 24px;
            color: #666;
            border-radius: 4px;
            transition: all 0.2s;
        }
        .original-link:hover {
            background-color: rgba(0, 119, 204, 0.1);
            color: #0077cc;
        }
        .original-link svg {
            width: 14px;
            height: 14px;
        }
        .toc-entry:hover {
            background-color: rgba(0, 0, 0, 0.03);
        }
        .title {
            color: #2156a5;
            text-decoration: none;
            font-size: 1.1em;
            display: block;
            word-wrap: break-word;
            overflow-wrap: break-word;
            max-width: 100%;
            width: fit-content;
            overflow: hidden;
        }
        .url-link {
            color: #0077cc;
            text-decoration: none;
            word-break: break-all;
        }
        .url-link:hover {
            text-decoration: underline;
            color: #0055aa;
        }
        .title.truncated {
            white-space: nowrap;
            text-overflow: ellipsis;
        }
        .title:hover { 
            text-decoration: underline;
        }
        .level-0 { 
            margin: 20px 0 10px 0; 
            font-size: 1.2em; 
            font-weight: bold;
            border-bottom: 1px solid #eee;
            padding-bottom: 5px;
        }
        .metadata {
            font-size: 0.85em;
            color: #666;
            position: absolute;
            background: #f5f5f5;
            border: 1px solid #ddd;
            padding: 5px 10px;
            border-radius: 4px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.1);
            z-index: 10;
            display: none;
            top: calc(100% + 5px);
            left: 20px;
            min-width: 200px;
            max-width: 600px;
            white-space: nowrap;
        }
        body.show-metadata .metadata {
            opacity: 0.8;
        }
        body.show-metadata .toc-entry:hover .metadata {
            opacity: 1;
        }
        .toc-entry:hover .metadata { display: block; }
        .section {
            margin: 20px 0;
            padding: 10px;
            background: #f8f9fa;
            border-radius: 5px;
        }
        .section-title {
            font-size: 1.3em;
            color: #333;
            margin-bottom: 10px;
            padding: 8px;
            border-bottom: 1px solid #ddd;
            border-radius: 4px;
            background-color: #f4f4f4;
        }
        button {
            padding: 8px 15px;
            background-color: #4285f4;
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
            transition: all 0.2s ease;
        }
        button:hover {
            background-color: #3367d6;
        }
        button.active {
            background-color: #1c3aa9;
            box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.2);
            position: relative;
        }
        button.active::after {
            content: '✓';
            position: absolute;
            right: 8px;
            top: 50%;
            transform: translateY(-50%);
            font-size: 12px;
        }
        @media (max-width: 768px) {
            .controls {
                flex-direction: column;
            }
            .controls button {
                width: 100%;
                margin: 2px 0;
                text-align: left;
                padding-left: 15px;
            }
            .toc {
                margin-left: 10px;
            }
            .metadata {
                max-width: 90%;
            }
        }
    """


def _generate_toc_entry(page: DocPage, index: int) -> str:
    """Генерирует HTML для одного элемента оглавления"""
    indentation = page.level * 30

    editions_html = ""
    if page.editions:
        badges = [
            f'<a class="edition-badge" href="page_{index:04d}/{filename}" title="Редакция: {label}">{label}</a>'
            for suffix, label, filename in page.editions
        ]
        editions_html = f'<div class="editions-badges">{"".join(badges)}</div>'

    return f"""
        <div class="toc-entry level-{page.level}" style="margin-left: {indentation}px;">
            <div class="entry-row">
                <a class="title" href="page_{index:04d}/page.html" title="{page.title}">{page.number} {page.title}</a>
                {editions_html}
                <div class="actions">
                    <a href="{page.url}" target="_blank" class="original-link" title="Открыть оригинальную страницу">
                        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
                            <polyline points="15 3 21 3 21 9"></polyline>
                            <line x1="10" y1="14" x2="21" y2="3"></line>
                        </svg>
                    </a>
                </div>
            </div>
            <div class="metadata">
                <div><strong>URL:</strong> <a href="{page.url}" target="_blank" class="url-link">{page.url}</a></div>
                <div><strong>Уровень:</strong> {page.level + 1}</div>
                <div><strong>Номер:</strong> {page.number}</div>
            </div>
        </div>"""


def _write_index_html(pages: List[DocPage]) -> None:
    """Формирует и записывает файл оглавления out/index.html"""
    toc_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Оглавление документации</title>
    <style>{_generate_html_styles()}</style>
</head>
<body>
    <h1>Оглавление документации</h1>
    
    <div class="controls">
        <button data-level="0" class="level-filter">Показать только уровень 1</button>
        <button data-level="1" class="level-filter">Показать до уровня 2</button>
        <button data-level="2" class="level-filter">Показать до уровня 3</button>
        <button data-level="20" class="level-filter active">Показать все уровни</button>
        <button id="toggle-metadata">Показать/скрыть метаданные</button>
    </div>
    
    <div class="toc">
"""

    current_level0 = None
    for i, page in enumerate(pages, 1):
        if page.level == 0:
            if current_level0 is not None:
                toc_html += "    </div>\n</div>\n"

            section_id = f"section_{i}"
            toc_html += f'<div class="section" id="{section_id}">\n'
            toc_html += f'    <div class="section-title">{page.number} {page.title}</div>\n'
            toc_html += '    <div class="section-content">\n'
            current_level0 = page.title

        toc_html += _generate_toc_entry(page, i)

    if current_level0 is not None:
        toc_html += "    </div>\n</div>\n"

    toc_html += """
    </div>
    
    <script>
    document.querySelectorAll('.level-filter').forEach(button => {
        button.addEventListener('click', function() {
            const maxLevel = parseInt(this.getAttribute('data-level'));
            
            document.querySelectorAll('.level-filter').forEach(btn => {
                btn.classList.remove('active');
            });
            this.classList.add('active');
            
            document.querySelectorAll('.toc-entry').forEach(elem => {
                const match = elem.className.match(/level-(\d+)/);
                if (match) {
                    const lvl = parseInt(match[1]);
                    elem.style.display = (lvl <= maxLevel) ? 'block' : 'none';
                }
            });
            
            localStorage.setItem('selectedLevel', maxLevel);
        });
    });
    
    document.getElementById('toggle-metadata').addEventListener('click', function() {
        const body = document.body;
        const isActive = body.classList.contains('show-metadata');
        
        if (isActive) {
            body.classList.remove('show-metadata');
            this.classList.remove('active');
            localStorage.setItem('showMetadata', 'false');
        } else {
            body.classList.add('show-metadata');
            this.classList.add('active');
            localStorage.setItem('showMetadata', 'true');
        }
    });
    
    document.addEventListener('DOMContentLoaded', function() {
        const savedLevel = localStorage.getItem('selectedLevel');
        if (savedLevel) {
            const levelButton = document.querySelector(`.level-filter[data-level="${savedLevel}"]`);
            if (levelButton) {
                levelButton.click();
            }
        }
        
        const showMetadata = localStorage.getItem('showMetadata');
        if (showMetadata === 'true') {
            document.getElementById('toggle-metadata').click();
        }
        
        const titles = document.querySelectorAll('.title');
        titles.forEach(title => {
            if (title.offsetWidth < title.scrollWidth) {
                title.classList.add('truncated');
            }
        });
    });
    </script>
</body>
</html>"""

    os.makedirs('out', exist_ok=True)
    with open(os.path.join('out', 'index.html'), 'w', encoding='utf-8') as f:
        f.write(toc_html)


def save_all_pages(browser: WebDriver, pages: List[DocPage], limit: Optional[int] = None) -> None:
    """Сохраняет все страницы документации, их вкладки редакций и перелинковывает их локально"""
    if limit is not None:
        pages = pages[:limit]
        print(f"Ограничение: будет сохранено {len(pages)} страниц")

    _write_index_html(pages)

    total = len(pages)
    for i, page in enumerate(pages, 1):
        try:
            if args.verbose:
                print(f"\nОбработка страницы {i}/{total}")
                print(f"Заголовок: {page.title}")
                print(f"Уровень: {page.level + 1}")
                print(f"URL: {page.url}")
            else:
                print(f"Обработка: {i}/{total} - {page.number} {page.title}")

            page_dir = os.path.join('out', f"page_{i:04d}")
            os.makedirs(page_dir, exist_ok=True)

            _navigate_to_its_url(browser, page.url)

            # Ищем все вкладки редакций внутри статьи (и #content:ID:suffix, и /db/.../content/ID/suffix)
            doc_id, main_suffix, extra_editions = _discover_article_editions(
                browser, page.url, "w_metadata_doc_frame"
            )

            # Сохраняем основную вкладку в page.html
            save_iframe_content(browser, "w_metadata_doc_frame", output_dir=page_dir, html_filename="page.html")
            simplify_image_paths(page_dir, html_filename="page.html")

            suffix_to_filename: Dict[str, str] = {}
            if main_suffix:
                suffix_to_filename[main_suffix] = "page.html"

            saved_editions_info = []
            for suffix, label, ed_url in extra_editions:
                safe_suffix = re.sub(r'[^a-zA-Z0-9_]', '_', suffix)
                ed_filename = f"page_{safe_suffix}.html"
                if args.verbose:
                    print(f"  -> Найдена вкладка редакции [{label}] ({suffix}): {ed_url}")
                else:
                    print(f"   + Вкладка редакции: {label} -> {ed_filename}")

                try:
                    _navigate_to_its_url(browser, ed_url)
                    save_iframe_content(browser, "w_metadata_doc_frame", output_dir=page_dir, html_filename=ed_filename)
                    simplify_image_paths(page_dir, html_filename=ed_filename)

                    suffix_to_filename[suffix] = ed_filename
                    page.editions.append((suffix, label, ed_filename))
                    saved_editions_info.append(f"{label} ({suffix}): {ed_url} -> {ed_filename}")
                except Exception as ed_err:
                    if args.verbose:
                        print(f"  -> Не удалось сохранить вкладку редакции {suffix}: {str(ed_err)}")

            # Переписываем внутренние ссылки /db/.../content/ID/suffix на локальные page_*.html
            if doc_id and len(suffix_to_filename) > 1:
                _rewrite_edition_links_in_dir(page_dir, doc_id, suffix_to_filename)

            with open(os.path.join(page_dir, 'metadata.txt'), 'w', encoding='utf-8') as f:
                f.write(f"Title: {page.title}\n")
                f.write(f"Number: {page.number}\n")
                f.write(f"Level: {page.level + 1}\n")
                f.write(f"URL: {page.url}\n")
                if saved_editions_info:
                    f.write("Editions:\n")
                    for line in saved_editions_info:
                        f.write(f"  - {line}\n")

        except Exception as e:
            print(f"Ошибка при сохранении страницы {page.title}: {str(e)}")
            continue

    _write_index_html(pages)


def save_iframe_content(browser: WebDriver, iframe_id: str, output_dir: str = 'out', html_filename: str = 'page.html') -> None:
    """Сохраняет содержимое iframe в указанный HTML-файл"""
    try:
        WebDriverWait(browser, 15).until(
            EC.frame_to_be_available_and_switch_to_it((By.ID, iframe_id))
        )

        try:
            WebDriverWait(browser, 15).until(
                lambda x: x.execute_script("return document.readyState") == "complete"
            )

            images_dir = os.path.join(output_dir, 'images')
            os.makedirs(images_dir, exist_ok=True)

            images = browser.find_elements(By.TAG_NAME, "img")
            processed_images = {}

            for img in images:
                src = img.get_attribute('src')
                if not src:
                    continue

                if src in processed_images:
                    new_src = processed_images[src]
                    img_path = f"images/{new_src}"
                else:
                    new_src = download_image(browser, img, images_dir)
                    if new_src:
                        img_path = f"images/{new_src}"
                        processed_images[src] = new_src
                    else:
                        continue

                browser.execute_script("""
                    arguments[0].setAttribute('src', arguments[1]);
                    if (arguments[0].hasAttribute('data-src')) {
                        arguments[0].setAttribute('data-src', arguments[1]);
                    }
                    arguments[0].classList.remove('incomplete');
                    var style = arguments[0].getAttribute('style');
                    if (style) {
                        var sizeStyles = [];
                        if (style.includes('width')) {
                            sizeStyles.push('width: ' + arguments[0].width + 'px');
                        }
                        if (style.includes('height')) {
                            sizeStyles.push('height: ' + arguments[0].height + 'px');
                        }
                        if (sizeStyles.length > 0) {
                            arguments[0].setAttribute('style', sizeStyles.join('; '));
                        } else {
                            arguments[0].removeAttribute('style');
                        }
                    }
                """, img, img_path)

            iframe_content = browser.page_source

            img_pattern = re.compile(r'<img[^>]*src="images/([^"]+)"[^>]*width="([^"]+)"[^>]*height="([^"]+)"[^>]*>')
            for match in img_pattern.finditer(iframe_content):
                src, width, height = match.groups()
                old_tag = match.group(0)
                new_tag = f'<img src="images/{src}" width="{width}" height="{height}" alt="">'
                iframe_content = iframe_content.replace(old_tag, new_tag)

            iframe_content = re.sub(
                r'src="[^"]*?/([^/"]+\.(png|jpg|gif|jpeg))"',
                r'src="images/\1"',
                iframe_content
            )

            if '<meta charset=' not in iframe_content and '<meta http-equiv="Content-Type"' not in iframe_content:
                iframe_content = re.sub(
                    r'<head[^>]*>',
                    r'<head>\n    <meta charset="utf-8">',
                    iframe_content
                )
            else:
                iframe_content = re.sub(
                    r'<meta[^>]*charset=[^>]*>',
                    r'<meta charset="utf-8">',
                    iframe_content
                )
                iframe_content = re.sub(
                    r'<meta[^>]*Content-Type[^>]*>',
                    r'<meta charset="utf-8">',
                    iframe_content
                )

            output_file = os.path.join(output_dir, html_filename)
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(iframe_content)

            post_process_html(output_file)

        finally:
            browser.switch_to.default_content()

    except TimeoutException as e:
        print("Ошибка: таймаут при ожидании iframe")
        if args.verbose:
            print(f"Детали: {str(e)}")
        raise
    except NoSuchFrameException as e:
        print("Ошибка: не удалось переключиться на iframe")
        if args.verbose:
            print(f"Детали: {str(e)}")
        raise


def clean_output_directory(directory: str = 'out') -> None:
    """Очищает каталог вывода, если он существует"""
    if os.path.exists(directory):
        print(f"Очистка каталога {directory}")
        shutil.rmtree(directory)
    os.makedirs(directory, exist_ok=True)


def post_process_html(html_file_path: str) -> bool:
    """Постобработка HTML файла для исправления путей к изображениям"""
    try:
        with open(html_file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        img_tag_pattern = re.compile(IMG_TAG_PATTERN)

        for match in img_tag_pattern.finditer(content):
            full_img_tag = match.group(0)
            src = match.group(1)

            if 'images/' not in src and ('/' in src or '%' in src or 'http' in src):
                filename = src.split('/')[-1]
                new_src = f"images/{filename}"
                new_img_tag = full_img_tag.replace(src, new_src)
                content = content.replace(full_img_tag, new_img_tag)

        content = re.sub(
            r'<img[^>]*?src="images/([^"]+)"[^>]*?width="([^"]*)"[^>]*?height="([^"]*)"[^>]*?>',
            r'<img src="images/\1" width="\2" height="\3" alt="">',
            content
        )

        content = re.sub(
            r'<img([^>]*?)class="[^"]*?incomplete[^"]*?"([^>]*?)>',
            r'<img\1\2>',
            content
        )

        encoded_path_pattern = re.compile(r'images/[^"]*?%[^"]*?\.(?:png|jpg|gif|jpeg)')
        for match in encoded_path_pattern.finditer(content):
            encoded_path = match.group(0)
            new_path = f"images/image{len(set(encoded_path_pattern.findall(content)))}.png"
            content = content.replace(encoded_path, new_path)

        content = re.sub(
            r'images/[^"]+?/([^/"]+\.(?:png|jpg|gif|jpeg))',
            r'images/\1',
            content
        )

        with open(html_file_path, 'w', encoding='utf-8') as f:
            f.write(content)

        return True
    except Exception as e:
        if not args.headless:
            print(f"Ошибка при постобработке HTML: {str(e)}")
        return False


def clean_img_tags(html_content: str) -> str:
    """Очищает теги img от ненужных атрибутов, оставляя только src, width, height и alt"""
    img_pattern = re.compile(IMG_TAG_PATTERN)
    replacements = {}

    for match in img_pattern.finditer(html_content):
        full_tag = match.group(0)
        src = match.group(1)

        if full_tag in replacements:
            continue

        width_match = re.search(r'width="([^"]+)"', full_tag)
        width = width_match.group(1) if width_match else ""

        height_match = re.search(r'height="([^"]+)"', full_tag)
        height = height_match.group(1) if height_match else ""

        if width and height:
            new_tag = f'<img src="{src}" width="{width}" height="{height}" alt="">'
        elif width:
            new_tag = f'<img src="{src}" width="{width}" alt="">'
        elif height:
            new_tag = f'<img src="{src}" height="{height}" alt="">'
        else:
            new_tag = f'<img src="{src}" alt="">'

        replacements[full_tag] = new_tag

    for old_tag, new_tag in replacements.items():
        html_content = html_content.replace(old_tag, new_tag)

    return html_content


def simplify_image_paths(page_dir: str, html_filename: str = 'page.html') -> bool:
    """Копирует все изображения из сложных путей в простую директорию img и обновляет указанный HTML-файл"""
    try:
        images_dir = os.path.join(page_dir, 'images')
        if not os.path.exists(images_dir):
            return False

        simple_images_dir = os.path.join(page_dir, 'img')
        os.makedirs(simple_images_dir, exist_ok=True)

        image_counter = len([f for f in os.listdir(simple_images_dir) if os.path.isfile(os.path.join(simple_images_dir, f))]) + 1
        path_mapping = {}

        for root, dirs, files in os.walk(images_dir):
            for file in files:
                if file.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                    src_path = os.path.join(root, file)
                    rel_path = os.path.relpath(src_path, images_dir)
                    _, ext = os.path.splitext(file)

                    new_filename = f"image{image_counter:03d}{ext}"
                    dst_path = os.path.join(simple_images_dir, new_filename)

                    shutil.copy2(src_path, dst_path)
                    path_mapping[rel_path] = new_filename
                    image_counter += 1

        html_file = os.path.join(page_dir, html_filename)
        if os.path.exists(html_file):
            with open(html_file, 'r', encoding='utf-8') as f:
                content = f.read()

            img_tags = re.findall(r'<img[^>]*?src="([^"]+)"[^>]*?>', content)
            new_paths = {}

            for src in img_tags:
                if src.startswith('images/'):
                    rel_path = src[7:]
                    if rel_path in path_mapping:
                        new_paths[src] = f"img/{path_mapping[rel_path]}"
                    else:
                        filename = os.path.basename(rel_path)
                        for old_path, new_name in path_mapping.items():
                            if os.path.basename(old_path) == filename:
                                new_paths[src] = f"img/{new_name}"
                                break
                        else:
                            new_name = f"image{len(new_paths) + 1:03d}{os.path.splitext(filename)[1]}"
                            new_paths[src] = f"img/{new_name}"

            for old_path, new_path in new_paths.items():
                content = content.replace(f'src="{old_path}"', f'src="{new_path}"')

            content = clean_img_tags(content)

            with open(html_file, 'w', encoding='utf-8') as f:
                f.write(content)

        return True
    except Exception as e:
        if args.verbose:
            print(f"Ошибка при упрощении путей к изображениям: {str(e)}")
        return False


def main():
    global args

    parser = argparse.ArgumentParser(
        description="Парсер документации 1С ИТС - инструмент для локального сохранения документации.",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--url', required=True, help='URL документации для загрузки (например, https://its.1c.ru/db/edtdoc)')
    parser.add_argument('--login', required=True, help='URL страницы авторизации (например, https://login.1c.ru/login)')
    parser.add_argument('--username', required=False, help='Имя пользователя (если не указано, берется из .env файла)')
    parser.add_argument('--password', required=False, help='Пароль пользователя (если не указано, берется из .env файла)')
    parser.add_argument('--limit', type=int, help='Ограничение количества страниц для загрузки (по умолчанию - все страницы)')
    parser.add_argument('--headless', action='store_true', help='Запуск браузера в фоновом режиме без отображения окна')
    parser.add_argument('--verbose', action='store_true', help='Включить расширенный вывод для отладки')
    args = parser.parse_args()

    if not args.username:
        args.username = os.environ.get('USERNAME')
    if not args.password:
        args.password = os.environ.get('PASSWORD')

    if not args.username or not args.password:
        raise ValueError("Необходимо указать username и password в аргументах или в файле .env")

    clean_output_directory()

    options = webdriver.ChromeOptions()
    if args.headless:
        options.add_argument('--headless=new')
        options.add_argument('--disable-gpu')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--no-sandbox')

    options.add_argument('--disable-extensions')
    options.add_argument('--disable-infobars')
    options.add_argument('--disable-notifications')
    options.add_argument('--disable-popup-blocking')
    options.add_argument('--blink-settings=imagesEnabled=true')
    options.page_load_strategy = 'eager'

    prefs = {
        'profile.default_content_setting_values.notifications': 2,
        'profile.managed_default_content_settings.images': 1,
        'disk-cache-size': 4096,
    }
    options.add_experimental_option('prefs', prefs)

    browser = webdriver.Chrome(options=options)
    browser.maximize_window()

    try:
        print("Авторизация...")
        browser.get(args.login)

        username_input = WebDriverWait(browser, 10).until(
            EC.presence_of_element_located((By.NAME, "username"))
        )
        password_input = browser.find_element(By.NAME, "password")

        username_input.send_keys(args.username)
        password_input.send_keys(args.password)

        login_button = browser.find_element(By.CSS_SELECTOR, "input[type='submit']")
        login_button.click()

        time.sleep(2)

        print("Загрузка документации...")
        browser.get(args.url)
        time.sleep(2)

        pages = extract_doc_structure(browser)

        if args.limit:
            print(f"Сохранение {args.limit} страниц...")
        else:
            print("Сохранение всех страниц...")

        save_all_pages(browser, pages, args.limit)
        print("Готово!")

    except Exception as e:
        print("Ошибка при выполнении")
        if args.verbose:
            print(f"Детали: {str(e)}")
            print("URL в момент ошибки:", browser.current_url)
    finally:
        browser.quit()


if __name__ == "__main__":
    main()
