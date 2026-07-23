#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DOCX 文章翻译前的非译元素保护 + 译后还原（python-docx 原地翻译）。

设计（依据 OOXML 结构与社媒反馈的 docx 翻译痛点）：
  - DOCX 超链接存于 document.xml.rels（<w:hyperlink r:id> 指向关系），
    其 URL 根本不在可见文本里 —— 因此 URL "天然"不被翻译，只需原地改显示文本 run 即可保链接。
  - 逐 run 抽取可见文本，每个 run 给一个 [[DOCX<gid>]] 占位行；译后按相同遍历顺序回填到原 run，
    段落/表格/标题/列表/字体样式（加粗/斜体/颜色）全部保真（只改 run 文本，不重建文档）。
  - 正文里出现的裸 URL（http(s)://）抽成 [[PROT<id>]]，译后还原，避免被译乱。
  - 跳过整段域（目录 TOC、PAGE 页码、REF 交叉引用等 w:fldChar 字段，含其显示结果），
    避免译乱结构或把页码数字改坏；译后如需刷新目录，在 Word 里右键"更新域"即可。
  - 还原前校验每个 [[DOCX#]] 恰好一次，并检测被整段删除的 [[PROT#]]（裸 URL 缺失即告警）；
    还原后检查无 [[PROT#]] 残留。还原采用"按 token 分段解析"，对译文中换行/空格鲁棒。

遍历覆盖范围：正文段落 + 表格单元格 + 页眉/页脚 + 脚注（含文本框 txbxContent 经通用递归自然覆盖）。

依赖：python-docx（本机 venv 已含）。
"""
import re
import json
import sys
import argparse
from docx import Document
from docx.oxml.ns import qn
from docx.text.run import Run as _Run

DOCX_TOKEN = "[[DOCX{}]]"
PROT_TOKEN = "[[PROT{}]]"
DOCX_RE = re.compile(r"\[\[DOCX(\d+)\]\]")
PROT_RE = re.compile(r"\[\[PROT(\d+)\]\]")
# 按 token 分段解析译文：每个 [[DOCX#]] 之后到下一个 [[DOCX#]] 或文末，为其译文。
# 对换行/空格鲁棒，不会因 LLM 折行而静默失败。
SEG_RE = re.compile(r"\[\[DOCX(\d+)\]\]([\s\S]*?)(?=\[\[DOCX(\d+)\]\]|$)")
# 裸 URL：排除集只含空白/引号/尖括号/方括号/全角/CJK（西里尔允许，见 protect_links.py 注释）
BARE_URL_RE = re.compile(r"https?://[^\s)<>\"'\]\u4e00-\u9fff\uff00-\uffef]+")


def get_hyperlink_url(hyperlink_el, part):
    """从 w:hyperlink 解析目标 URL（r:id 关系 或 w:anchor 书签）。"""
    rel = hyperlink_el.get(qn("r:id")) or hyperlink_el.get(qn("w:anchor"))
    if not rel:
        return None
    try:
        return part.rels[rel].target_ref
    except (KeyError, AttributeError):
        return rel  # 书签锚点，原样保留


def _run_fldchar_type(run_el):
    """If the run contains a w:fldChar, return its type; else None."""
    fld = run_el.find(qn("w:fldChar"))
    if fld is None:
        return None
    return fld.get(qn("w:fldCharType"))


def _run_instrtext(run_el):
    instr = run_el.find(qn("w:instrText"))
    return instr.text if instr is not None else None


def _read_field(children, i):
    """Given index i of a begin w:fldChar run, return (field_type, separate_idx, end_idx).

    field_type is 'HYPERLINK' if the field code starts with HYPERLINK, else 'OTHER'.
    separate_idx is the index of the 'separate' fldChar run (or None).
    end_idx is the index of the matching 'end' fldChar run.

    NOTE: field instructions live inside w:r runs (w:instrText child) and field
    boundaries are w:fldChar runs, so detection must look *inside* each w:r.
    """
    code_parts = []
    j = i + 1
    separate_idx = None
    while j < len(children):
        c = children[j]
        if c.tag == qn("w:r"):
            ft = _run_fldchar_type(c)
            if ft == "separate":
                separate_idx = j
                j += 1
                break
            elif ft == "end":
                return ("OTHER", None, j)
            it = _run_instrtext(c)
            if it is not None:
                code_parts.append(it)
        j += 1
    code = " ".join(code_parts).strip()
    ftype = "HYPERLINK" if code.upper().startswith("HYPERLINK") else "OTHER"
    depth = 1
    end_idx = j
    while end_idx < len(children):
        c = children[end_idx]
        if c.tag == qn("w:r"):
            ft = _run_fldchar_type(c)
            if ft == "begin":
                depth += 1
            elif ft == "end":
                depth -= 1
                if depth == 0:
                    break
        end_idx += 1
    return (ftype, separate_idx, end_idx)


def _field_code(children, i):
    """Collect instrText of a field between the begin (index i) and separate/end."""
    parts = []
    j = i + 1
    while j < len(children):
        c = children[j]
        if c.tag == qn("w:r"):
            ft = _run_fldchar_type(c)
            if ft in ("separate", "end"):
                break
            it = _run_instrtext(c)
            if it is not None:
                parts.append(it)
        j += 1
    return " ".join(parts).strip()


def _hyperlink_url_from_code(code):
    """Extract URL from a HYPERLINK field code like: HYPERLINK \"https://...\" ."""
    m = re.search(r'"([^"]+)"', code)
    return m.group(1) if m else None


def _walk(element, part, doc, hlink_url, out):
    """Recursively collect translatable (run_el, hlink_url) pairs in document order.

    Fields are handled as units:
      - HYPERLINK fields: the display-text runs ARE translated (the URL lives in
        the field code and is never touched); nested content is recursed.
      - Other fields (TOC / PAGE / REF / ...): the whole field is skipped so its
        generated result text is neither translated nor corrupted.
    """
    children = list(element)
    i = 0
    while i < len(children):
        child = children[i]
        tag = child.tag
        if tag == qn("w:hyperlink"):
            url = get_hyperlink_url(child, part)
            _walk(child, part, doc, url, out)
        elif tag == qn("w:r"):
            ft = _run_fldchar_type(child)
            if ft == "begin":
                ftype2, sep, end = _read_field(children, i)
                if ftype2 == "HYPERLINK":
                    code = _field_code(children, i)
                    url = _hyperlink_url_from_code(code) if code else hlink_url
                    start = sep + 1 if sep is not None else i + 1
                    for k in range(start, end):
                        ch = children[k]
                        if ch.tag == qn("w:r"):
                            if is_translatable(ch, doc):
                                out.append((ch, url, 0))
                        elif ch.tag == qn("w:hyperlink"):
                            _walk(ch, part, doc, get_hyperlink_url(ch, part), out)
                        # other node types inside display content: ignored
                # non-hyperlink field (or hyperlink end): skip the whole field
                i = end
                i += 1
                continue
            # separate / end fldChar run or a normal run
            if is_translatable(child, doc):
                out.append((child, hlink_url, 0))
        else:
            _walk(child, part, doc, hlink_url, out)
        i += 1


def is_translatable(run_el, doc):
    """域指令 run（w:instrText）与纯空白 run 跳过；其余为可译。"""
    if run_el.find(qn("w:instrText")) is not None:
        return False
    try:
        text = _Run(run_el, doc).text or ""
    except Exception:
        return False
    return bool(text.strip())


def get_parts(doc):
    """返回有序 [(part_id, part, root_element), ...]，覆盖正文/页眉页脚/脚注。

    注意：python-docx 中 _Header/_Footer 没有 .element 属性，
    其根元素是 hdr.part.element（HeaderPart 的根）。
    """
    parts = []
    parts.append(("body", doc.part, doc.element.body))
    for si, section in enumerate(doc.sections):
        try:
            hdr = section.header
            if hdr is not None and hdr.part is not None:
                parts.append((f"header{si}", hdr.part, hdr.part.element))
        except Exception:
            pass
        try:
            ftr = section.footer
            if ftr is not None and ftr.part is not None:
                parts.append((f"footer{si}", ftr.part, ftr.part.element))
        except Exception:
            pass
    try:
        fn_part = doc.part.footnotes_part
        if fn_part is not None:
            parts.append(("footnotes", fn_part, fn_part.element))
    except Exception:
        pass
    return parts


def iter_translatable(part_id, part, root, doc):
    """按文档顺序产出 (local_idx, run_el, hlink_url) 仅可译 run。

    _walk 已按字段类型处理：HYPERLINK 字段的显示文本会被收录，其余字段
    （TOC/PAGE/REF...）整体跳过，故此处只需再过滤空白/域指令 run。
    """
    runs = []
    _walk(root, part, doc, None, runs)
    local = 0
    for run_el, hlink_url, _fdepth in runs:
        if not is_translatable(run_el, doc):
            continue
        yield local, run_el, hlink_url
        local += 1


def extract(docx_path, src_path, store_path):
    doc = Document(docx_path)
    parts = get_parts(doc)
    fragments = []
    prot_counter = [0]
    src_lines = []

    for part_id, part, root in parts:
        for local, run_el, hlink_url in iter_translatable(part_id, part, root, doc):
            gid = len(fragments) + 1
            text = _Run(run_el, doc).text or ""
            prot_map = {}

            def mask(m, _pc=prot_counter):
                _pc[0] += 1
                tok = PROT_TOKEN.format(_pc[0])
                prot_map[tok] = m.group(0)
                return tok

            masked = BARE_URL_RE.sub(mask, text)
            fragments.append({
                "part": part_id,
                "idx": local,
                "hlink_url": hlink_url,
                "prot_map": prot_map,
            })
            src_lines.append(DOCX_TOKEN.format(gid) + masked)

    open(src_path, "w", encoding="utf-8", newline="").write("\n".join(src_lines) + "\n")
    json.dump({"fragments": fragments}, open(store_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"Extracted {len(fragments)} translatable runs -> {src_path}; store -> {store_path}")


def restore(docx_path, src_path, store_path, out_path):
    doc = Document(docx_path)
    store = json.load(open(store_path, encoding="utf-8"))
    fragments = store["fragments"]
    order_index = {(f["part"], f["idx"]): i + 1 for i, f in enumerate(fragments)}

    # 解析译文：按 [[DOCX#]] 分段，对换行/空格鲁棒（不依赖行首锚定）。
    raw = open(src_path, encoding="utf-8", newline="").read()
    translated = {}
    for m in SEG_RE.finditer(raw):
        gid = int(m.group(1))
        if gid < 1 or gid > len(fragments):
            continue
        # 去尾部换行（token 间的换行属于分隔符，非 run 文本；run 文本本身不含换行）
        text = m.group(2).rstrip("\r\n")
        frag = fragments[gid - 1]
        for tok, url in frag.get("prot_map", {}).items():
            text = text.replace(tok, url)
        translated[gid] = text

    # 校验：每个 [[DOCX#]] 在 source 恰好一次
    issues = []
    for gid in range(1, len(fragments) + 1):
        cnt = len(re.findall(r"\[\[DOCX%d\]\]" % gid, raw))
        if cnt != 1:
            issues.append(f"[[DOCX{gid}]] 出现 {cnt} 次(应为1)")

    # 检测缺失的 [[PROT#]]（被模型整段删掉 → 裸 URL 丢失）
    expected_prots = set()
    for f in fragments:
        expected_prots.update(f.get("prot_map", {}).keys())
    missing_prots = [t for t in expected_prots if t not in raw]

    # 回填
    for part_id, part, root in get_parts(doc):
        for local, run_el, hlink_url in iter_translatable(part_id, part, root, doc):
            gid = order_index.get((part_id, local))
            if gid and gid in translated:
                _Run(run_el, doc).text = translated[gid]

    doc.save(out_path)

    # 残留检测（还原后不应再有 [[PROT#]]）
    leftovers = sorted(set(PROT_RE.findall("\n".join(translated.values()))))
    if issues:
        print("WARNING: token 计数异常: " + "; ".join(issues), file=sys.stderr)
    if missing_prots:
        print(f"WARNING: {len(missing_prots)} 个 [[PROT]] 占位符在译文中缺失(裸 URL 可能已丢失): {missing_prots}", file=sys.stderr)
    if leftovers:
        print(f"WARNING: {len(leftovers)} 个 [[PROT]] 未还原: {leftovers}", file=sys.stderr)
    if not issues and not missing_prots and not leftovers:
        print(f"All {len(fragments)} DOCX tokens OK -> {out_path}")


def main():
    ap = argparse.ArgumentParser(description="Protect/restore DOCX non-translatable elements for LLM translation.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract")
    e.add_argument("--in", "-i", dest="infile", required=True)
    e.add_argument("--out", "-o", dest="outfile", required=True)
    e.add_argument("--store", "-s", dest="storefile", required=True)
    r = sub.add_parser("restore")
    r.add_argument("--docx", "-d", dest="docxfile", required=True,
                   help="原始 docx（提供结构用于原地回填）")
    r.add_argument("--in", "-i", dest="infile", required=True,
                   help="译文（protect 后交给 LLM 翻译得到的文本）")
    r.add_argument("--store", "-s", dest="storefile", required=True)
    r.add_argument("--out", "-o", dest="outfile", required=True)
    args = ap.parse_args()
    if args.cmd == "extract":
        extract(args.infile, args.outfile, args.storefile)
    else:
        restore(args.docxfile, args.infile, args.storefile, args.outfile)


if __name__ == "__main__":
    main()
