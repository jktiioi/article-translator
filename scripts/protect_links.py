#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文章翻译前的非译元素占位符保护，译后还原 + 校验。
解决社交反馈最高频痛点：链接丢失、排版错乱、占位符被改/漏/重。

设计要点（依据 htmltranslate.com 等权威实践）：
  - 只保护"非译部分"——链接的 URL/href、代码、HTML 属性值、裸 URL。
  - 链接锚文本、图片 alt 文本、HTML 标签间可见文本 均为用户可读内容，
    应保持可见、交给模型翻译；仅 URL 抽成 [[PROT]] 占位符。
  - 还原前校验每个占位符恰好出现一次（缺失/重复即告警），还原后无残留。

保护顺序（先长后短，先整体后局部）：
  代码块 -> 行内代码 -> 图片(护URL) -> 行内链接(护URL) -> 引用定义(护URL)
  -> HTML href/src(护URL) -> 裸URL
"""
import re
import json
import sys
import argparse

TOKEN_RE = re.compile(r"\[\[PROT(\d+)\]\]")
PLACEHOLDER = "[[PROT{}]]"


def protect(text: str):
    store = {}
    n = [0]

    def stash(token: str) -> str:
        n[0] += 1
        key = PLACEHOLDER.format(n[0])
        store[key] = token
        return key

    # 1. 代码块（整体，不译）
    text = re.sub(r"```[\s\S]*?```", lambda m: stash(m.group(0)), text)
    # 2. 行内代码（整体，不译）
    text = re.sub(r"`[^`\n]+`", lambda m: stash(m.group(0)), text)
    # 3. 图片：保留 ![alt]，仅护 URL
    text = re.sub(r"(!\[[^\]]*\]\()([^)]*)(\))",
                  lambda m: m.group(1) + stash(m.group(2)) + m.group(3), text)
    # 4. 行内链接：保留 [text]，仅护 URL（跳过已是占位符的情况，避免嵌套）
    text = re.sub(r"\]\((?!\[\[PROT)[^)]*\)",
                  lambda m: "](" + stash(m.group(0)[2:-1]) + ")", text)
    # 5. 引用式定义 [key]: url —— 排除脚注 [^1]:，仅护 url，key 保留
    text = re.sub(r"(^\[(?![\^])[^\]]+\]:[ \t]*)(\S+)",
                  lambda m: m.group(1) + stash(m.group(2)), text, flags=re.MULTILINE)
    # 6. HTML 的 href/src 属性值（仅护 URL，标签与可见文本保留可译）
    # 注意：开引号(group2)需在还原时同时放在 URL 前后，否则会丢开头引号
    text = re.sub(r'((?:href|src)\s*=\s*)("|\')(.*?)\2',
                  lambda m: m.group(1) + m.group(2) + stash(m.group(3)) + m.group(2), text)
    # 7. 裸 URL（正文里无括号包裹的 http(s)://）
    # 排除集只含：空白/引号/尖括号/方括号/全角/CJK。
    # 注意：西里尔(\u0400-\u04ff)不在此排除——俄文以空格分词，IDN 西里尔域名
    # 在俄文正文里以空格为界，可安全整体护住；若排除它，西里尔 IDN 会漏护被译乱。
    # CJK 保留排除——中日文无词间空格，"详见https://x说明"这类无空格紧接会吞掉相邻正文。
    text = re.sub(r"https?://[^\s)<>\"'\]\u4e00-\u9fff\uff00-\uffef]+",
                  lambda m: stash(m.group(0)), text)
    return text, store


def restore(text: str, store: dict):
    # 还原前：每个占位符应恰好出现一次（缺失=漏译/误删，重复=模型篡改）
    issues = []
    for key in store:
        cnt = text.count(key)
        if cnt != 1:
            issues.append(f"{key} 出现 {cnt} 次(应为1)")
    final = text
    for key, val in store.items():
        final = final.replace(key, val)
    leftovers = sorted(set(TOKEN_RE.findall(final)))
    return final, issues, leftovers


def main():
    ap = argparse.ArgumentParser(
        description="Protect/restore non-translatable tokens for LLM translation.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("protect")
    p.add_argument("--in", "-i", dest="infile", required=True)
    p.add_argument("--out", "-o", dest="outfile", required=True)
    p.add_argument("--store", "-s", dest="storefile", required=True)

    r = sub.add_parser("restore")
    r.add_argument("--in", "-i", dest="infile", required=True)
    r.add_argument("--store", "-s", dest="storefile", required=True)
    r.add_argument("--out", "-o", dest="outfile", required=True)

    args = ap.parse_args()

    if args.cmd == "protect":
        raw = open(args.infile, encoding="utf-8", newline="").read()
        prot, store = protect(raw)
        open(args.outfile, "w", encoding="utf-8", newline="").write(prot)
        json.dump(store, open(args.storefile, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"Protected {len(store)} tokens -> {args.outfile}; store -> {args.storefile}")
    else:
        prot = open(args.infile, encoding="utf-8", newline="").read()
        store = json.load(open(args.storefile, encoding="utf-8"))
        final, issues, leftovers = restore(prot, store)
        try:
            open(args.outfile, "w", encoding="utf-8", newline="").write(final)
        except OSError as e:
            print(f"ERROR: 无法写入 {args.outfile}: {e}", file=sys.stderr)
        if issues:
            print("WARNING: 占位符计数异常: " + "; ".join(issues), file=sys.stderr)
        if leftovers:
            print(f"WARNING: {len(leftovers)} 占位符未还原: {leftovers}", file=sys.stderr)
        if not issues and not leftovers:
            print(f"All {len(store)} placeholders OK (each exactly once) -> {args.outfile}")


if __name__ == "__main__":
    main()
