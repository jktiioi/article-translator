# article-translator

<p align="right"><a href="#article-translator">English</a> · <a href="#中文简介">中文</a></p>

A standalone Python toolkit for translating long-form articles while **preserving layout, hyperlinks, numbers, and styles**.

It runs an LLM-driven translation pipeline (draft → reflect → improve) with placeholder-based protection of non-translatable elements and an MQM-based review pass. Translation languages are *parameters*, not hardcoded examples: supply a glossary and the same workflow handles any language pair.

---

## Features

- **Non-translatable protection by mechanism, not by model discipline.** Links, code blocks, inline code, images, HTML tags, bare URLs, and reference-style links are extracted into `[[PROT#]]` placeholders before translation and restored (and verified) afterward. The model never sees the raw non-translatable content.
- **In-place Word `.docx` translation.** Every run in body / tables / headers / footers / footnotes is translated; fonts, bold/italic, tables, and layout are preserved. Hyperlink URLs live in `document.xml.rels`, so they are never touched by the translation — only the display text is translated in place.
- **Markdown / plain text support** via the same placeholder approach.
- **Configurable languages.** Ships ready for EN↔ZH and EN↔RU. Add a language by providing a glossary CSV; the workflow does not change.
- **MQM review.** Self-review against the six most relevant MQM dimensions (Accuracy, Terminology, Fluency, Style, Locale, Design).
- **Optional bilingual output** (source ∥ target) for human verification.

---

## Requirements

- Python **3.10+**
- [`python-docx`](https://python-docx.readthedocs.io/) (for `.docx` support only; Markdown/text needs no extra dependency)

---

## Install

Clone the repository and install the dependencies:

```bash
git clone https://github.com/jktiioi/article-translator.git
cd article-translator
pip install -r requirements.txt
```

`SKILL.md` is the instruction file for agent-based use; place the folder into your agent's skills directory to enable it as a skill.

---

## Usage (standalone)

The scripts work standalone — you bring your own LLM to translate the extracted text, keeping the `[[...]]` placeholders intact.

### Word `.docx`

```bash
# 1. Extract translatable runs into src.txt, store originals in store.json
python scripts/protect_docx.py extract --in article.docx --out src.txt --store store.json

# 2. Translate src.txt with your LLM.
#    Keep every [[DOCX#]] placeholder exactly once, in order.

# 3. Restore the translation back into a new .docx
python scripts/protect_docx.py restore --docx article.docx \
    --in translated.txt --store store.json --out article_translated.docx
```

### Markdown / plain text

```bash
# 1. Protect links / code / HTML / bare URLs into placeholders
python scripts/protect_links.py protect --in article.md --out protected.md --store store.json

# 2. Translate protected.md (keep every [[PROT#]] placeholder intact)

# 3. Restore
python scripts/protect_links.py restore --in translated.md --store store.json --out article_translated.md
```

---

## Workflow (the methodology)

1. **Prepare** — confirm source/target language, audience, tone, domain; prepare a glossary (CSV: `source,target`).
2. **Protect** — extract non-translatable elements into placeholders (`extract` / `protect`).
3. **Draft** — translate the protected text; lock tone/audience/domain in the system prompt; keep all placeholders.
4. **Reflect** — self-review against `references/mqm_checklist.md`; emit issue JSON.
5. **Improve** — revise per reflection; accuracy issues must be cross-checked against the glossary / rules.
6. **Restore + verify** — `restore` the translation; then verify the *rendered* document (not just placeholder counts): link/field display text keeps a single space from adjacent words and punctuation; decimal separators and units follow the target locale; punctuation/symbols are consistent throughout.

> AI output is a draft. For critical content (contracts, medical, legal, financial), always have a human do the final review regardless of score.

---

## Repository layout

```
article-translator/
├── SKILL.md                      # Skill instruction file (Chinese; the methodology)
├── scripts/
│   ├── protect_docx.py           # .docx in-place translation: extract / restore
│   └── protect_links.py          # Markdown/text placeholder protect / restore
├── references/
│   ├── prompt_templates.md       # draft / reflect / improve prompt templates
│   ├── mqm_checklist.md          # MQM six-dimension review checklist
│   └── glossary_template.csv     # glossary template (language-agnostic)
├── requirements.txt
├── LICENSE
└── README.md
```

---

## License

[MIT](LICENSE) — free to use, modify, and redistribute.

---

## 中文简介

一个用于翻译长文/博客/文档的独立 Python 工具，**保留排版、超链接、数字与样式**。它用「占位符保护非译元素 + 三步法（初译→反思→改进）+ MQM 审校」的工程化流程，语言是参数而非示例——提供术语表即可翻译任意语言对。

- 安装：`git clone https://github.com/jktiioi/article-translator.git`，然后 `pip install -r requirements.txt`（仅 `.docx` 需要 `python-docx`）
- `SKILL.md` 为 agent 用法说明文件（中文）；将其所在文件夹放入你的 agent 技能目录即可作为技能启用。
- 用法见上方命令。
