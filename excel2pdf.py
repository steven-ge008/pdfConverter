#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
excel2pdf.py — Excel(xlsx) 转 PDF 工具（数据不丢失、版式规整）

针对"报关草单 / 合同 / 装箱单 / 商业发票"类单据工作簿做了分页与版式优化：
不强行分页——页面能承载的内容保持自然流动（当前模板为 4 页：报关草单、
合同、装箱单、商业发票各 1 页），商品行数增多时页数自然增加；
并保证所有单元格数据、合并单元格与嵌入图片（公章/签名）完整保留。

原理：
  1. 解析 xlsx（仅用 Python 标准库），统计每个 sheet 的真实数据范围
     （剔除纯样式空单元格造成的"幽灵"行列，例如报关草单 Z16:Z47）；
  2. 为缺少打印区域的 sheet 自动补打印区域；已有打印区域装不下数据/图片时自动扩展；
  3. 按 sheet 配置启用 fitToPage（缩放至指定页宽/页高）并覆盖页边距
     （复刻 Excel 原样导出的留白），使表格居中、不贴边、不被横向切断；
  4. 把修改写入临时副本（不改动原文件），调用 LibreOffice 无头模式转 PDF；
  5. 校验输出并抽查关键文本是否完整。

用法：
  python3 excel2pdf.py 输入.xlsx
  python3 excel2pdf.py 输入.xlsx -o 输出.pdf --pages 4

依赖：本机需安装 LibreOffice（soffice）；
      可选 poppler（pdftotext / pdfimages，用于数据完整性抽查，缺失时自动跳过）。
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

NS_MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
NS_R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
NS_XDR = 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'
EMU_PER_PT = 12700.0  # 1pt = 12700 EMU

# ---------------------------------------------------------------------------
# 每个 sheet 的页面配置（按 sheet 名精确匹配；未匹配的 sheet 用 DEFAULT 配置）
#   fit_width / fit_height : 缩放至 N 页宽 / N 页高（0 表示不限制，按自然分页）
#   margins               : 页边距覆盖（英寸），只覆盖给出的方向，其余沿用原文件；
#                           数值按 LibreOffice 渲染校准，用于复刻 Excel 按原始
#                           scale 打印导出的留白效果，避免内容贴边或拉满页宽
#   breaks                : 手动分页符 [(关键字, before|after), ...]，before/after 表示
#                           在包含该关键字的行之前/之后分页；默认不配置——内容放得下就
#                           不强行分页，仅在确需固定分页点时使用
# ---------------------------------------------------------------------------
SHEET_PAGE_CONFIG = {
    '报关草单': dict(fit_width=1, fit_height=1,
                     margins=dict(left=0.65, right=0.65, top=0.278)),  # 顶部留白约 20pt，不贴边
    '合同': dict(fit_width=1, fit_height=1, margins=dict(right=0.96)),      # 整表缩放为 1 页
    '装箱单': dict(fit_width=1, fit_height=1, margins=dict(right=1.12)),
    '商业发票': dict(fit_width=1, fit_height=1, margins=dict(right=1.22)),   # 整表缩放为 1 页
}
DEFAULT_PAGE_CONFIG = dict(fit_width=1, fit_height=0)

DEFAULT_TARGET_PAGES = 0  # 0 = 不校验页数（内容自然分页，仅校验数据完整性）


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def col_letter(n):
    """列号(1 起)转字母：1->A, 26->Z, 27->AA"""
    s = ''
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def col_index(s):
    """列字母转列号(1 起)"""
    n = 0
    for ch in s:
        n = n * 26 + ord(ch) - 64
    return n


def resolve_part(base_dir, target):
    """把关系目标（相对路径）解析为 zip 内部部件路径"""
    if target.startswith('/'):
        segs = target.lstrip('/').split('/')
    else:
        segs = (base_dir + '/' + target).split('/')
    parts = []
    for seg in segs:
        if seg in ('', '.'):
            continue
        if seg == '..':
            if parts:
                parts.pop()
        else:
            parts.append(seg)
    return '/'.join(parts)


def set_attr(tag, name, value):
    """给自闭合标签设置属性（存在则替换，不存在则追加）"""
    if re.search(rf'{name}="[^"]*"', tag):
        return re.sub(rf'{name}="[^"]*"', f'{name}="{value}"', tag)
    if not tag.endswith('/>'):
        raise ValueError(f'非自闭合标签: {tag}')
    return tag[:-2] + f' {name}="{value}"/>'


def find_soffice(explicit=None):
    """定位 soffice 可执行文件"""
    for cand in ([explicit] if explicit else []) + [
        shutil.which('soffice'),
        '/Applications/LibreOffice.app/Contents/MacOS/soffice',
        '/usr/bin/soffice',
        '/usr/local/bin/soffice',
    ]:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


# ---------------------------------------------------------------------------
# xlsx 读取与分析
# ---------------------------------------------------------------------------

def load_shared_strings(zf):
    if 'xl/sharedStrings.xml' not in zf.namelist():
        return []
    root = ET.fromstring(zf.read('xl/sharedStrings.xml'))
    return [''.join(t.text or '' for t in si.iter(f'{{{NS_MAIN}}}t'))
            for si in root.findall(f'{{{NS_MAIN}}}si')]


def cell_text(c, shared):
    """取单元格的实际文本值（共享字符串 / 内联字符串 / 数字原文）"""
    t = c.get('t')
    v = c.find(f'{{{NS_MAIN}}}v')
    if t == 's':
        if v is not None and v.text is not None and v.text.isdigit():
            i = int(v.text)
            return shared[i] if i < len(shared) else ''
        return ''
    if t == 'inlineStr':
        is_el = c.find(f'{{{NS_MAIN}}}is')
        if is_el is not None:
            return ''.join(x.text or '' for x in is_el.iter(f'{{{NS_MAIN}}}t'))
        return ''
    if v is not None:
        return v.text or ''
    return ''


def load_sheets(zf):
    """按 workbook 顺序返回 sheet 列表（含部件路径）"""
    wb = ET.fromstring(zf.read('xl/workbook.xml'))
    rels = ET.fromstring(zf.read('xl/_rels/workbook.xml.rels'))
    rel_map = {r.get('Id'): r.get('Target') for r in rels}
    sheets = []
    for sh in wb.find(f'{{{NS_MAIN}}}sheets').findall(f'{{{NS_MAIN}}}sheet'):
        target = rel_map.get(sh.get(f'{{{NS_R}}}id'), '')
        sheets.append({
            'name': sh.get('name'),
            'part': resolve_part('xl', target),
            'state': sh.get('state') or 'visible',
        })
    return sheets


def load_print_areas(zf, sheet_count):
    """读取已定义的打印区域：{sheet 序号: (ref 原文, max_row, max_col)}"""
    xml = zf.read('xl/workbook.xml').decode('utf-8')
    areas = {}
    for m in re.finditer(
            r'<definedName name="_xlnm\.Print_Area" localSheetId="(\d+)"[^>]*>([^<]+)</definedName>', xml):
        idx = int(m.group(1))
        if idx >= sheet_count:
            continue
        ref = m.group(2).strip()
        cells = re.findall(r'\$?([A-Z]+)\$?(\d+)', ref)
        if not cells:
            continue
        max_col = max(col_index(c) for c, _ in cells)
        max_row = max(int(r) for _, r in cells)
        areas[idx] = (ref, max_row, max_col)
    return areas


def analyze_sheet(zf, part, shared):
    """分析单个 sheet：内容边界（含合并单元格扩展）、行高列宽、单元格值"""
    root = ET.fromstring(zf.read(part))
    values = {}
    max_row = max_col = 0
    for c in root.iter(f'{{{NS_MAIN}}}c'):
        v = cell_text(c, shared)
        if not v.strip():
            continue
        m = re.match(r'([A-Z]+)(\d+)', c.get('r') or '')
        if not m:
            continue
        col, row = col_index(m.group(1)), int(m.group(2))
        values[(row, col)] = v
        max_row, max_col = max(max_row, row), max(max_col, col)

    # 合并单元格：左上角有值的，整体纳入内容边界（避免打印区域切断合并列）
    merges = []
    mc = root.find(f'{{{NS_MAIN}}}mergeCells')
    if mc is not None:
        for m in mc.findall(f'{{{NS_MAIN}}}mergeCell'):
            rng = re.findall(r'([A-Z]+)(\d+)', m.get('ref'))
            if len(rng) == 2:
                r1, c1 = int(rng[0][1]), col_index(rng[0][0])
                r2, c2 = int(rng[1][1]), col_index(rng[1][0])
                merges.append((r1, c1, r2, c2))
                if values.get((r1, c1), '').strip():
                    max_row, max_col = max(max_row, r2), max(max_col, c2)

    # 行高 / 列宽（用于推算图片覆盖范围）
    row_h = {}
    col_w = {}
    default_row, default_col = 14.4, 9.0
    sfp = root.find(f'{{{NS_MAIN}}}sheetFormatPr')
    if sfp is not None:
        default_row = float(sfp.get('defaultRowHeight') or 14.4)
        default_col = float(sfp.get('defaultColWidth') or 9.0)
    for row in root.iter(f'{{{NS_MAIN}}}row'):
        if row.get('ht'):
            row_h[int(row.get('r'))] = float(row.get('ht'))
    cols = root.find(f'{{{NS_MAIN}}}cols')
    if cols is not None:
        for c in cols.findall(f'{{{NS_MAIN}}}col'):
            mn, mx = int(c.get('min')), int(c.get('max'))
            w = float(c.get('width') or default_col)
            for i in range(mn, mx + 1):
                col_w[i] = w

    return dict(max_row=max_row, max_col=max_col, values=values, merges=merges,
                row_h=row_h, col_w=col_w, default_row=default_row, default_col=default_col)


def _rows_touched(start_row, pt, row_h, default_row):
    """自 start_row 行顶起，向下覆盖 pt 磅所触及的最后一行（6pt 容差，忽略轻微溢出）"""
    rem, r = pt, start_row
    while rem > 6.0:
        h = row_h.get(r, default_row) or default_row
        rem -= h
        if rem <= 6.0:
            break
        r += 1
    return r


def _cols_touched(start_col, pt, col_w, default_col):
    """自 start_col 列左缘起，向右覆盖 pt 磅所触及的最后一列（6pt 容差）"""
    rem, c = pt, start_col
    while rem > 6.0:
        w = (col_w.get(c, default_col) or default_col) * 7 + 5  # 字符宽 -> px
        rem -= w * 0.75                                            # px -> pt
        if rem <= 6.0:
            break
        c += 1
    return c


def drawing_bounds(zf, sheet_part, geom):
    """计算 sheet 上图片（公章等）锚点覆盖的 (max_row, max_col)"""
    base = os.path.dirname(sheet_part)
    rels_part = f"{base}/_rels/{os.path.basename(sheet_part)}.rels"
    if rels_part not in zf.namelist():
        return 0, 0, 0
    rels = ET.fromstring(zf.read(rels_part))
    max_row = max_col = pic_count = 0
    for r in rels:
        if not (r.get('Type') or '').endswith('/drawing'):
            continue
        dpart = resolve_part(base, r.get('Target'))
        if dpart not in zf.namelist():
            continue
        droot = ET.fromstring(zf.read(dpart))
        for anchor in droot:
            fr = anchor.find(f'{{{NS_XDR}}}from')
            if fr is None:
                continue
            if anchor.find(f'{{{NS_XDR}}}pic') is not None:
                pic_count += 1
            r1 = int(fr.find(f'{{{NS_XDR}}}row').text) + 1
            c1 = int(fr.find(f'{{{NS_XDR}}}col').text) + 1
            to = anchor.find(f'{{{NS_XDR}}}to')
            if to is not None:
                r2 = int(to.find(f'{{{NS_XDR}}}row').text) + 1
                c2 = int(to.find(f'{{{NS_XDR}}}col').text) + 1
            else:
                ext = anchor.find(f'{{{NS_XDR}}}ext')
                cx = int(ext.get('cx', 0)) / EMU_PER_PT if ext is not None else 0
                cy = int(ext.get('cy', 0)) / EMU_PER_PT if ext is not None else 0
                r2 = _rows_touched(r1, cy, geom['row_h'], geom['default_row'])
                c2 = _cols_touched(c1, cx, geom['col_w'], geom['default_col'])
            max_row, max_col = max(max_row, r2), max(max_col, c2)
    return max_row, max_col, pic_count


# ---------------------------------------------------------------------------
# xlsx 修改（字符串手术，保持其余内容原样）
# ---------------------------------------------------------------------------

def patch_sheet_xml(xml, cfg, break_rows):
    """启用 fitToPage、设置 fitToWidth/fitToHeight、覆盖页边距、插入手动分页符"""
    # 1) sheetPr 中启用 fitToPage
    if '<pageSetUpPr' not in xml:
        if '</sheetPr>' in xml:
            xml = xml.replace('</sheetPr>', '<pageSetUpPr fitToPage="1"/></sheetPr>', 1)
        elif '<sheetPr/>' in xml:
            xml = xml.replace('<sheetPr/>', '<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>', 1)
        else:
            m = re.search(r'<worksheet[^>]*>', xml)
            xml = xml[:m.end()] + '<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>' + xml[m.end():]

    # 2) pageMargins：按配置覆盖页边距（英寸）
    margins = cfg.get('margins') or {}
    m = re.search(r'<pageMargins[^>]*/>', xml)
    if m:
        pm = m.group(0)
        for k, v in margins.items():
            pm = set_attr(pm, k, v)
        xml = xml.replace(m.group(0), pm, 1)
    elif margins:
        pm = '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
        for k, v in margins.items():
            pm = set_attr(pm, k, v)
        ps_pos = re.search(r'<pageSetup[^>]*/>', xml)
        if ps_pos:
            xml = xml[:ps_pos.start()] + pm + xml[ps_pos.start():]
        else:
            xml = xml.replace('</worksheet>', pm + '</worksheet>')

    # 3) pageSetup：设置 fitToWidth/fitToHeight（fitToPage 生效时移除 scale）
    fw, fh = cfg['fit_width'], cfg['fit_height']
    m = re.search(r'<pageSetup[^>]*/>', xml)
    if m:
        ps = re.sub(r'\s*scale="[^"]*"', '', m.group(0))
        ps = set_attr(ps, 'fitToWidth', fw)
        ps = set_attr(ps, 'fitToHeight', fh)
        xml = xml.replace(m.group(0), ps, 1)
    else:
        ps = f'<pageSetup fitToWidth="{fw}" fitToHeight="{fh}"/>'
        mm = re.search(r'<pageMargins[^>]*/>', xml)
        if mm:
            xml = xml[:mm.end()] + ps + xml[mm.end():]
        else:
            xml = xml.replace('</worksheet>', ps + '</worksheet>')

    # 4) 手动分页符（schema 顺序：pageSetup -> headerFooter -> rowBreaks -> ... -> drawing）
    break_rows = sorted(set(break_rows))
    if break_rows:
        brks = ''.join(f'<brk id="{r}" max="16383" man="1"/>' for r in break_rows)
        rb = (f'<rowBreaks count="{len(break_rows)}" manualBreakCount="{len(break_rows)}">'
              f'{brks}</rowBreaks>')
        if re.search(r'<rowBreaks[^>]*>.*?</rowBreaks>', xml, re.S):
            xml = re.sub(r'<rowBreaks[^>]*>.*?</rowBreaks>', lambda _: rb, xml, flags=re.S)
        else:
            pos = None
            m2 = re.search(r'<headerFooter[^>]*/>|<headerFooter.*?</headerFooter>', xml, re.S)
            if m2:
                pos = m2.end()
            else:
                m3 = re.search(r'<pageSetup[^>]*/>', xml)
                pos = m3.end() if m3 else xml.rfind('</worksheet>')
            xml = xml[:pos] + rb + xml[pos:]
    return xml


def quote_sheet_name(name):
    return "'" + name.replace("'", "''") + "'"


def upsert_print_area(wb_xml, sheet_idx, sheet_name, ref):
    """新增或替换某个 sheet 的打印区域定义"""
    entry = (f'<definedName name="_xlnm.Print_Area" localSheetId="{sheet_idx}">'
             f'{quote_sheet_name(sheet_name)}!{ref}</definedName>')
    pat = rf'<definedName name="_xlnm\.Print_Area" localSheetId="{sheet_idx}"[^>]*>[^<]*</definedName>'
    if re.search(pat, wb_xml):
        return re.sub(pat, lambda _: entry, wb_xml)
    if '<definedNames>' in wb_xml:
        return wb_xml.replace('<definedNames>', '<definedNames>' + entry, 1)
    return wb_xml.replace('</sheets>', '</sheets><definedNames>' + entry + '</definedNames>', 1)


# ---------------------------------------------------------------------------
# 字体处理
# ---------------------------------------------------------------------------

# 中文字体名的判别依据：字体名含 CJK 统一表意字符，或匹配常见中文字体的英文名
CJK_FONT_RE = re.compile(
    r'[\u3400-\u4dbf\u4e00-\u9fff]'
    r'|sim(sun|hei)|nsimsun|kaiti|fangsong|lisu|youyuan|huawen'
    r'|microsoft yahei|ms yahei|st(song|hei|kai|fang|xihei|zhongsong|libian)',
    re.IGNORECASE)


def is_cjk_font_name(name):
    return bool(name) and bool(CJK_FONT_RE.search(name))


def xml_attr_escape(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def declared_fonts(zin):
    """统计 xlsx 各部件声明的字体（styles.xml + sharedStrings 富文本 + theme 主题字体）"""
    cnt = {}

    def bump(name):
        cnt[name] = cnt.get(name, 0) + 1

    names = set(zin.namelist())
    if 'xl/styles.xml' in names:
        for n in re.findall(r'<name val="([^"]*)"', zin.read('xl/styles.xml').decode('utf-8')):
            bump(n)
    if 'xl/sharedStrings.xml' in names:
        for n in re.findall(r'<rFont val="([^"]*)"', zin.read('xl/sharedStrings.xml').decode('utf-8')):
            bump(n)
    for part in sorted(p for p in names if re.fullmatch(r'xl/theme/theme\d+\.xml', p)):
        theme = zin.read(part).decode('utf-8')
        for n in re.findall(r'<a:latin typeface="([^"]+)"', theme):
            bump(n)
        for n in re.findall(r'<a:ea typeface="([^"]+)"', theme):
            bump(n)
    return cnt


def replace_cjk_font_decls(xml, font):
    """把 styles/sharedStrings 里声明为中文字体名的 <name>/<rFont> 统一替换为指定字体"""
    esc = xml_attr_escape(font)

    def repl(m):
        return m.group(1) + esc + m.group(3) if is_cjk_font_name(m.group(2)) else m.group(0)

    xml = re.sub(r'(<name val=")([^"]*)(")', repl, xml)
    xml = re.sub(r'(<rFont val=")([^"]*)(")', repl, xml)
    return xml


def replace_theme_ea(xml, font):
    """theme 的东亚字体槽位（非空时）替换为指定字体；西文槽位保留"""
    esc = xml_attr_escape(font)
    return re.sub(r'(<a:ea typeface=")([^"]+)(")', lambda m: m.group(1) + esc + m.group(3), xml)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def build_fixed_workbook(src_path, dst_path, log, font=None):
    """生成修正了页面设置的副本，返回 (需校验的单元格文本集合, 图片总数)

    font 非 None 时，把声明为中文字体名的单元格字体统一替换为该字体，
    避免 LibreOffice 在本机缺字体时随机 fallback。
    """
    with zipfile.ZipFile(src_path) as zin:
        shared = load_shared_strings(zin)
        sheets = load_sheets(zin)
        if not sheets:
            raise RuntimeError('工作簿中没有 sheet')
        print_areas = load_print_areas(zin, len(sheets))

        fonts_declared = declared_fonts(zin)
        if fonts_declared:
            detail = '、'.join(f'{n}({c})' for n, c in
                              sorted(fonts_declared.items(), key=lambda kv: -kv[1]))
            log(f'  声明字体: {detail}')
        if font:
            log(f'  中文字体统一替换为: {font}')

        wb_xml = zin.read('xl/workbook.xml').decode('utf-8')
        sheet_xml_patches = {}
        all_values = []
        total_pics = 0

        for idx, sh in enumerate(sheets):
            part = sh['part']
            if part not in zin.namelist():
                continue
            geom = analyze_sheet(zin, part, shared)
            all_values.extend(geom['values'].values())

            draw_row, draw_col, pics = drawing_bounds(zin, part, geom)
            total_pics += pics

            # 需要覆盖的范围 = 单元格内容 ∪ 图片锚点
            need_row = max(geom['max_row'], draw_row)
            need_col = max(geom['max_col'], draw_col)

            # 打印区域：缺失则新建；已有但装不下数据/图片则扩展
            area_action = ''
            if idx in print_areas:
                _, old_row, old_col = print_areas[idx]
                if need_row > old_row or need_col > old_col:
                    ref = f'$A$1:${col_letter(max(old_col, need_col))}${max(old_row, need_row)}'
                    wb_xml = upsert_print_area(wb_xml, idx, sh['name'], ref)
                    area_action = f'扩展打印区域 -> {ref}'
                else:
                    area_action = f'沿用打印区域 {print_areas[idx][0]}'
            elif need_row and need_col:
                ref = f'$A$1:${col_letter(need_col)}${need_row}'
                wb_xml = upsert_print_area(wb_xml, idx, sh['name'], ref)
                area_action = f'新建打印区域 -> {ref}'
            else:
                area_action = '空 sheet，跳过打印区域'

            # 分页配置（含按关键字定位的手动分页符）
            cfg = dict(DEFAULT_PAGE_CONFIG, **SHEET_PAGE_CONFIG.get(sh['name'], {}))
            break_rows = []
            for marker, pos in cfg.get('breaks', []):
                for (row, _col), v in geom['values'].items():
                    if marker in v:
                        break_rows.append(row - 1 if pos == 'before' else row)
                        break

            xml = zin.read(part).decode('utf-8')
            xml = patch_sheet_xml(xml, cfg, break_rows)
            sheet_xml_patches[part] = xml
            margin_note = ''
            if cfg.get('margins'):
                margin_note = '；边距 ' + '、'.join(f'{k}={v}"' for k, v in cfg['margins'].items())
            brk_note = (f'；行 {"、".join(map(str, sorted(set(break_rows))))} 后分页'
                        if break_rows else '')
            log(f"  [{sh['name']}] fitToPage {cfg['fit_width']}x{cfg['fit_height'] or '∞'}"
                f"{brk_note}{margin_note}；{area_action}")

        with zipfile.ZipFile(dst_path, 'w', zipfile.ZIP_DEFLATED) as zout:
            for item in zin.namelist():
                data = zin.read(item)
                if item == 'xl/workbook.xml':
                    data = wb_xml.encode('utf-8')
                elif item in sheet_xml_patches:
                    data = sheet_xml_patches[item].encode('utf-8')
                elif font and item in ('xl/styles.xml', 'xl/sharedStrings.xml'):
                    data = replace_cjk_font_decls(data.decode('utf-8'), font).encode('utf-8')
                elif font and re.fullmatch(r'xl/theme/theme\d+\.xml', item):
                    data = replace_theme_ea(data.decode('utf-8'), font).encode('utf-8')
                zout.writestr(item, data)
    return all_values, total_pics


def convert_pdf(soffice, xlsx_path, outdir, profile_dir):
    uri = Path(profile_dir).as_uri()
    cmd = [soffice, '--headless', '--norestore', '--nologo',
           f'-env:UserInstallation={uri}',
           '--convert-to', 'pdf:calc_pdf_Export', '--outdir', outdir, xlsx_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise RuntimeError('LibreOffice 转换超时（>300 秒）')
    pdf = os.path.join(outdir, os.path.splitext(os.path.basename(xlsx_path))[0] + '.pdf')
    if not os.path.exists(pdf):
        raise RuntimeError(f'LibreOffice 未生成 PDF。\nstdout: {proc.stdout}\nstderr: {proc.stderr}')
    return pdf


def pdf_page_count(pdf_path):
    with open(pdf_path, 'rb') as f:
        return len(re.findall(rb'/Type\s*/Page[^s]', f.read()))


def pdf_plain_text(pdf_path):
    exe = shutil.which('pdftotext')
    if not exe:
        return None
    proc = subprocess.run([exe, pdf_path, '-'], capture_output=True, text=True)
    return proc.stdout


def pdf_image_placements(pdf_path):
    """统计 PDF 中图像的绘制次数（同一图像跨页/多处引用会重复绘制，均计数）"""
    exe = shutil.which('pdfimages')
    if not exe:
        return None
    proc = subprocess.run([exe, '-list', pdf_path], capture_output=True, text=True)
    count = 0
    for line in proc.stdout.splitlines()[2:]:
        parts = line.split()
        if len(parts) >= 12 and parts[2] == 'image':
            count += 1
    return count


def pdf_embedded_fonts(pdf_path):
    """pdffonts 读取 PDF 实际嵌入的字体（去子集前缀、按出现顺序去重）；未安装返回 None"""
    exe = shutil.which('pdffonts')
    if not exe:
        return None
    proc = subprocess.run([exe, pdf_path], capture_output=True, text=True)
    fonts = []
    for line in proc.stdout.splitlines()[2:]:
        parts = line.split()
        if parts:
            name = re.sub(r'^[A-Z]{6}\+', '', parts[0])
            if name not in fonts:
                fonts.append(name)
    return fonts


def normalize_text(s):
    return re.sub(r'[\s,，、]+', '', s)


def collect_tokens(values):
    """把单元格文本拆成可抽查的短 token（过长/过短的跳过）"""
    tokens, skipped = set(), 0
    for v in values:
        for piece in re.split(r'[\n|]', v):
            p = piece.strip()
            if len(p) < 2:
                continue
            if len(p) > 20:
                skipped += 1
                continue
            tokens.add(p)
    return tokens, skipped


def main():
    ap = argparse.ArgumentParser(
        description='Excel(xlsx) 转 PDF 工具：数据不丢失、版式规整、内容自然分页（不强分页）')
    ap.add_argument('input', help='输入 xlsx 文件')
    ap.add_argument('-o', '--output', help='输出 PDF 路径（默认与输入同目录同名）')
    ap.add_argument('--pages', type=int, default=DEFAULT_TARGET_PAGES,
                    help='期望页数，0 表示不校验（默认 0，内容自然分页）')
    ap.add_argument('--no-verify', action='store_true', help='跳过数据完整性抽查')
    ap.add_argument('--keep-temp', action='store_true', help='保留临时文件（调试用）')
    ap.add_argument('--soffice', help='指定 soffice 可执行文件路径')
    ap.add_argument('--font', help='把中文字体（宋体/微软雅黑等）统一替换为指定字体，'
                    '如 "PingFang SC"，避免 LibreOffice 随机替换缺失字体；默认不替换')
    args = ap.parse_args()

    src = args.input
    if not os.path.isfile(src):
        sys.exit(f'错误：找不到输入文件 {src}')

    soffice = find_soffice(args.soffice)
    if not soffice:
        sys.exit('错误：未找到 LibreOffice（soffice）。请安装 LibreOffice 或用 --soffice 指定路径。')

    out = args.output or os.path.splitext(src)[0] + '.pdf'
    log = print

    log(f'输入: {src}')
    log('步骤 1/3 分析工作簿并修正页面设置...')
    tmpdir = tempfile.mkdtemp(prefix='excel2pdf_')
    try:
        fixed_xlsx = os.path.join(tmpdir, os.path.splitext(os.path.basename(src))[0] + '.xlsx')
        values, pic_count = build_fixed_workbook(src, fixed_xlsx, log, font=args.font or None)

        log('步骤 2/3 调用 LibreOffice 转换 PDF...')
        pdf_tmp = convert_pdf(soffice, fixed_xlsx, tmpdir, os.path.join(tmpdir, 'lo_profile'))
        shutil.move(pdf_tmp, out)

        log('步骤 3/3 校验输出...')
        pages = pdf_page_count(out)
        log(f'  页数: {pages}' + (f'（期望 {args.pages}）' if args.pages else ''))
        page_ok = (args.pages == 0) or (pages == args.pages)

        text_ok, missing = True, []
        img_ok, img_note = True, ''
        if not args.no_verify:
            text = pdf_plain_text(out)
            if text is None:
                log('  （未安装 pdftotext，跳过文本抽查）')
            else:
                tokens, skipped = collect_tokens(values)
                norm = normalize_text(text)
                missing = sorted(t for t in tokens if normalize_text(t) not in norm)
                text_ok = not missing
                log(f'  文本抽查: {len(tokens)} 个关键值'
                    f'{"，全部命中" if text_ok else f"，缺失 {len(missing)} 个"}'
                    f'（另有 {skipped} 个超长文本未抽查）')
                if missing:
                    log('  缺失明细: ' + ' | '.join(missing[:10]) + (' ...' if len(missing) > 10 else ''))
            img_draws = pdf_image_placements(out)
            if img_draws is None:
                log('  （未安装 pdfimages，跳过图片抽查）')
            elif pic_count:
                img_ok = img_draws >= pic_count
                img_note = f'图片抽查: 原文件 {pic_count} 张，PDF 中绘制 {img_draws} 次' \
                           + ('' if img_ok else '（疑似丢失）')
                log(f'  {img_note}')

        embedded_fonts = pdf_embedded_fonts(out)
        if embedded_fonts is None:
            log('  （未安装 pdffonts，跳过字体信息）')
        else:
            log('  PDF 实际字体: ' + ('、'.join(embedded_fonts) or '（无）'))

        if page_ok and text_ok and img_ok:
            log(f'完成: {out}（{pages} 页）')
            return 0
        if not page_ok:
            log(f'失败: 页数为 {pages}，与期望 {args.pages} 不符。PDF 已生成: {out}')
        if not text_ok:
            log('警告: 有关键文本未在 PDF 中找到，请人工核对（也可能为换行/格式导致的误报）。')
        if not img_ok:
            log(f'警告: {img_note}')
        return 1
    finally:
        if args.keep_temp:
            log(f'临时文件保留于: {tmpdir}')
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
