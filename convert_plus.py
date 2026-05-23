#!/usr/bin/env python3
import html
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import html2text
import yaml

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


WP_NS = '{http://wordpress.org/export/1.2/}'
DC_NS = '{http://purl.org/dc/elements/1.1/}'
CONTENT_NS = '{http://purl.org/rss/1.0/modules/content/}'

MEDIA_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp', '.tif', '.tiff',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.zip',
    '.mp3', '.m4a', '.wav', '.ogg', '.mp4', '.m4v', '.mov', '.webm',
}


def safe_get(item, path):
    el = item.find(path)
    return el.text if el is not None and el.text is not None else ''


def parse_wxr(file_path):
    tree = ET.parse(file_path)
    root = tree.getroot()
    return root.findall('.//item')


def extract_metadata(item):
    title = safe_get(item, 'title') or 'Untitled'
    wp_date = safe_get(item, f'{WP_NS}post_date')
    author = safe_get(item, f'{DC_NS}creator') or 'Unknown'
    status = safe_get(item, f'{WP_NS}post_status') or 'publish'
    post_type = safe_get(item, f'{WP_NS}post_type') or 'post'

    categories = item.findall('category')
    taxonomy = {}
    for c in categories:
        domain = c.get('domain')
        term = c.text
        if not term:
            continue
        if domain == 'category':
            taxonomy.setdefault('categories', []).append(term)
        elif domain == 'post_tag':
            taxonomy.setdefault('tags', []).append(term)

    return {
        'title': title,
        'date': wp_date,
        'author': author,
        'status': status,
        'type': post_type,
        'taxonomy': taxonomy,
    }


def convert_html_to_markdown(html_content):
    if not html_content:
        return ''
    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = False
    h.body_width = 0
    h.use_automatic_links = False
    return h.handle(html_content)


def generate_slug(title):
    slug = re.sub(r'[^\w\s]', '', title.lower())
    slug = re.sub(r'\s+', '-', slug)
    return slug.strip('-')[:100] or 'post'


def generate_frontmatter(metadata):
    frontmatter = {'title': metadata['title']}
    if metadata['date']:
        frontmatter['date'] = metadata['date']
    frontmatter['published'] = metadata['status'] == 'publish'
    if metadata['author'] != 'Unknown':
        frontmatter['author'] = metadata['author']

    taxonomy = {}
    if metadata['taxonomy'].get('categories'):
        taxonomy['category'] = metadata['taxonomy']['categories']
    if metadata['taxonomy'].get('tags'):
        taxonomy['tag'] = metadata['taxonomy']['tags']
    if taxonomy:
        frontmatter['taxonomy'] = taxonomy

    if metadata['type'] == 'post':
        frontmatter['type'] = 'post'
    return frontmatter


def post_id(item):
    return safe_get(item, f'{WP_NS}post_id')


def collect_attachment_urls(items):
    attachments = {}
    for item in items:
        if safe_get(item, f'{WP_NS}post_type') != 'attachment':
            continue

        parent_id = safe_get(item, f'{WP_NS}post_parent')
        url = safe_get(item, f'{WP_NS}attachment_url') or safe_get(item, 'guid')
        if parent_id and url:
            attachments.setdefault(parent_id, []).append(url)
    return attachments


def extract_media_urls(html_content, extra_urls=None):
    urls = set(extra_urls or [])
    if not html_content:
        return []

    decoded = html.unescape(html_content)
    patterns = [
        r'''(?:src|href)=["']([^"']+)["']''',
        r'''(?:srcset)=["']([^"']+)["']''',
        r'''https?://[^\s"'<>]+\.(?:jpg|jpeg|png|gif|webp|svg|bmp|tif|tiff|pdf|docx?|xlsx?|pptx?|zip|mp3|m4a|wav|ogg|mp4|m4v|mov|webm)(?:\?[^\s"'<>]*)?''',
    ]

    for pattern in patterns:
        for match in re.findall(pattern, decoded, flags=re.IGNORECASE):
            if ',' in match and ' ' in match:
                for srcset_part in match.split(','):
                    candidate = srcset_part.strip().split(' ')[0]
                    add_media_url(urls, candidate)
            else:
                add_media_url(urls, match)

    return sorted(urls)


def add_media_url(urls, candidate):
    candidate = html.unescape(candidate or '').strip()
    if not candidate.startswith(('http://', 'https://')):
        return

    parsed = urllib.parse.urlparse(candidate)
    suffix = Path(parsed.path).suffix.lower()
    if '/wp-content/uploads/' in parsed.path or suffix in MEDIA_EXTENSIONS:
        urls.add(urllib.parse.urlunparse(parsed._replace(fragment='')))


def safe_media_filename(url, used_names):
    parsed = urllib.parse.urlparse(url)
    raw_name = urllib.parse.unquote(Path(parsed.path).name) or 'media'
    stem = Path(raw_name).stem
    suffix = Path(raw_name).suffix

    safe_stem = re.sub(r'[^\w.-]+', '-', stem, flags=re.UNICODE).strip('.-') or 'media'
    safe_suffix = re.sub(r'[^A-Za-z0-9.]', '', suffix) or '.bin'
    filename = f'{safe_stem[:90]}{safe_suffix.lower()}'

    counter = 1
    while filename.lower() in used_names:
        filename = f'{safe_stem[:80]}-{counter}{safe_suffix.lower()}'
        counter += 1

    used_names.add(filename.lower())
    return filename


def download_media(url, destination):
    request = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'Mozilla/5.0 WordPress-to-Grav converter',
            'Accept': '*/*',
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            destination.write_bytes(response.read())
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        logging.warning(f"Could not download media {url}: {exc}")
        return False


def download_and_rewrite_media(markdown_content, media_urls, post_dir):
    url_to_filename = {}
    used_names = set()

    for url in media_urls:
        filename = safe_media_filename(url, used_names)
        destination = post_dir / filename
        if download_media(url, destination):
            url_to_filename[url] = filename

    for url, filename in url_to_filename.items():
        markdown_content = markdown_content.replace(url, filename)
        markdown_content = markdown_content.replace(html.escape(url), filename)

    return markdown_content, len(url_to_filename), len(media_urls) - len(url_to_filename)


def create_post_dir(metadata, output_dir, position):
    slug = generate_slug(metadata['title'])
    folder_name = f'{position:02d}.{slug}'
    post_dir = Path(output_dir) / folder_name

    counter = 1
    while post_dir.exists():
        folder_name = f'{position:02d}.{slug}_{counter}'
        post_dir = Path(output_dir) / folder_name
        counter += 1

    post_dir.mkdir(parents=True, exist_ok=False)
    return post_dir


def write_grav_post(metadata, markdown_content, post_dir):
    frontmatter = generate_frontmatter(metadata)
    yaml_str = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=False)
    content = f'---\n{yaml_str}---\n\n{markdown_content}'
    filepath = post_dir / 'item.md'
    filepath.write_text(content, encoding='utf-8')
    logging.info(f'Created: {filepath}')


def convert_wxr_to_grav(xml_file, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    items = parse_wxr(xml_file)
    attachments_by_parent = collect_attachment_urls(items)
    logging.info(f'Found {len(items)} items in WXR file')

    processed = 0
    skipped = 0
    downloaded = 0
    failed_media = 0

    for i, item in enumerate(items):
        item_type = safe_get(item, f'{WP_NS}post_type') or 'post'
        if item_type != 'post':
            skipped += 1
            continue

        meta = extract_metadata(item)
        html_content = safe_get(item, f'{CONTENT_NS}encoded')
        if not html_content:
            logging.warning(f'Item {i} has no content. Skipping.')
            skipped += 1
            continue

        processed += 1
        post_dir = create_post_dir(meta, output_dir, processed)

        markdown = convert_html_to_markdown(html_content)
        media_urls = extract_media_urls(html_content, attachments_by_parent.get(post_id(item), []))
        markdown, ok_count, fail_count = download_and_rewrite_media(markdown, media_urls, post_dir)
        downloaded += ok_count
        failed_media += fail_count

        write_grav_post(meta, markdown, post_dir)

    logging.info(
        f'Conversion complete: {processed} posts created, {skipped} skipped, '
        f'{downloaded} media files downloaded, {failed_media} media downloads failed'
    )


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print('Usage: python convert_plus.py <wordpress_xml_file> <output_directory>')
        sys.exit(1)
    convert_wxr_to_grav(sys.argv[1], sys.argv[2])
