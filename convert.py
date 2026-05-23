#!/usr/bin/env python3
import xml.etree.ElementTree as ET
import html2text
import yaml
import os
from pathlib import Path
import re
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# XML Namespace definitions for ElementTree
WP_NS = '{http://wordpress.org/export/1.2/}'
DC_NS = '{http://purl.org/dc/elements/1.1/}'
CONTENT_NS = '{http://purl.org/rss/1.0/modules/content/}'


def safe_get(item, path):
    """Safely get text from an XML element, return empty string if not found."""
    el = item.find(path)
    return el.text if el is not None else ''


def parse_wxr(file_path):
    tree = ET.parse(file_path)
    root = tree.getroot()
    items = root.findall('.//item')
    return items


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
        'taxonomy': taxonomy
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
    frontmatter = {}
    frontmatter['title'] = metadata['title']
    if metadata['date']:
        frontmatter['date'] = metadata['date']
    frontmatter['published'] = metadata['status'] == 'publish'
    if metadata['author'] != 'Unknown':
        frontmatter['author'] = metadata['author']
    if metadata['taxonomy']:
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


def write_grav_post(metadata, markdown_content, output_dir, position):
    slug = generate_slug(metadata['title'])
    folder_name = f"{position:02d}.{slug}"
    post_dir = Path(output_dir) / folder_name
    filepath = post_dir / "item.md"
    
    # Handle duplicate ordered folders without overwriting existing conversions.
    counter = 1
    while post_dir.exists():
        folder_name = f"{position:02d}.{slug}_{counter}"
        post_dir = Path(output_dir) / folder_name
        filepath = post_dir / "item.md"
        counter += 1
        
    frontmatter = generate_frontmatter(metadata)
    yaml_str = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=False)
    content = f"---\n{yaml_str}---\n\n{markdown_content}"
    post_dir.mkdir(parents=True, exist_ok=False)
    filepath.write_text(content, encoding='utf-8')
    logging.info(f"✅ Created: {filepath}")


def convert_wxr_to_grav(xml_file, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    items = parse_wxr(xml_file)
    logging.info(f"Found {len(items)} items in WXR file")
    
    processed = 0
    skipped = 0
    
    for i, item in enumerate(items):
        post_type = safe_get(item, f'{WP_NS}post_type') or 'post'
        if post_type != 'post':
            logging.info(f"⏭️ Skipping non-post item {i} (type: {post_type})")
            skipped += 1
            continue
            
        meta = extract_metadata(item)
        html_content = safe_get(item, f'{CONTENT_NS}encoded')
        if not html_content:
            logging.warning(f"⚠️ Item {i} has no content. Skipping.")
            skipped += 1
            continue
            
        markdown = convert_html_to_markdown(html_content)
        processed += 1
        write_grav_post(meta, markdown, output_dir, processed)
        
    logging.info(f"📊 Conversion complete: {processed} posts created, {skipped} skipped")


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 3:
        print("Usage: python convert_wxr_to_grav.py <wordpress_xml_file> <output_directory>")
        sys.exit(1)
    convert_wxr_to_grav(sys.argv[1], sys.argv[2])
