import os
import json
import zipfile
import tempfile
import re
import shutil
from collections import defaultdict

# --- CONFIGURATION ---
ACTIVE_RULES = {"v5", "v1", "adj-i", "vs", "vk", "vz"}
POS_MAPPING = {
    "v5r": "v5", "v5k": "v5", "v5m": "v5", "v5s": "v5", "v5g": "v5", 
    "v5t": "v5", "v5b": "v5", "v5n": "v5", "v5u": "v5", "v5u-s": "v5",
    "v5aru": "v5", "v1-s": "v1", "vs-i": "vs", "vs-s": "vs", "vs-c": "vs"
}
CORE_POS = {
    "n", "n-adv", "n-pr", "n-pref", "n-suf", "n-t", "num", "pn", "pref", "prt", "suf",
    "v1", "v1-s", "v5aru", "v5b", "v5g", "v5k", "v5k-s", "v5m", "v5n", "v5r", "v5r-i",
    "v5s", "v5t", "v5u", "v5u-s", "v5uru", "vi", "vk", "vn", "vr", "vs", "vs-c", "vs-i", 
    "vs-s", "vt", "vz", "adj-i", "adj-ix", "adj-na", "adj-no", "adj-pn", "adj-t", "adv", 
    "adv-to", "aux", "aux-adj", "aux-v", "conj", "cop", "ctr", "exp", "int"
}
ALLOWED_SPECIAL = { 
    "arch", "col", "derog", "euph", "fam", "hon", "hum", "joc", "pol", "sl", "vulg", 
    "rare", "dated", "obs", "hist", "uk", "id", "proverb", "yoji", "phil", "gramm"
}

def get_text_recursive(obj):
    if isinstance(obj, str): return obj
    if isinstance(obj, list):
        results = []
        for x in obj:
            res = get_text_recursive(x)
            if isinstance(x, dict) and x.get("tag") in ["td", "th"]:
                results.append(f"|{res}|")
            else:
                results.append(res)
        return "".join(results)
    if isinstance(obj, dict):
        return get_text_recursive(obj.get("content", ""))
    return ""

def parse_structured_content(content_list):
    glossary_items, references = [], []
    if not isinstance(content_list, list): content_list = [content_list]
    for block in content_list:
        if not isinstance(block, dict): continue
        if block.get("tag") == "a":
            ref_target = get_text_recursive(block).strip().replace("|", "")
            if ref_target: references.append(ref_target)
            continue
        dtype = block.get("data", {}).get("content", "")
        if dtype == "glossary":
            inner = block.get("content", [])
            items = inner if isinstance(inner, list) else [inner]
            for li in items:
                txt = get_text_recursive(li).strip().replace("|", "")
                if txt: glossary_items.append(txt)
        elif "content" in block:
            g, r = parse_structured_content(block["content"])
            if g: glossary_items.append(g)
            references.extend(r)
    return " | ".join(glossary_items), references

def process_header(kanji_head, reading_head, forms_list):
    seen_readings = {reading_head} if reading_head else set()
    readings = [reading_head] if reading_head else []
    seen_kanji, kanji_variants = set(), []

    # Non-greedy wildcard inside parens catches anything (including spaces/symbols)
    # Standalone symbols handled at the end
    regex_pattern = r"（.*?）|\(.*?\)|[㊒㊚∅★⛬▼⚠🅁]"

    clean_k_head = re.sub(regex_pattern, "", kanji_head).strip()
    
    for f in forms_list:
        parts = [p.strip() for p in f.split("|") if p.strip()]
        for p in parts:
            clean_p = re.sub(regex_pattern, "", p).strip()
            if not clean_p: continue
            if re.fullmatch(r"[\u3040-\u309F\u30A0-\u30FF]+", clean_p):
                if clean_p not in seen_readings:
                    readings.append(clean_p)
                    seen_readings.add(clean_p)
            else:
                if clean_p not in seen_kanji and clean_p != reading_head:
                    seen_kanji.add(clean_p)
                    kanji_variants.append(clean_p)

    if clean_k_head and not re.fullmatch(r"[\u3040-\u309F\u30A0-\u30FF]+", clean_k_head):
        if clean_k_head not in kanji_variants:
            kanji_variants.insert(0, clean_k_head)

    r_str = "・".join(readings)
    k_str = "・".join(kanji_variants)
    if not kanji_variants: return r_str
    if not r_str: return f"【{k_str}】"
    return f"{r_str}【{k_str}】"

def convert_new_to_old(input_dir, output_dir):
    if not os.path.exists(output_dir): os.makedirs(output_dir)
    for filename in os.listdir(input_dir):
        if not (filename.endswith(".json") and filename.startswith("term_bank")): continue
        with open(os.path.join(input_dir, filename), 'r', encoding='utf-8') as f:
            new_data = json.load(f)

        entries = defaultdict(lambda: {"forms": [], "senses": [], "score": 0, "seq": 0})
        for row in new_data:
            kanji, reading, pos_field, _, score, content, seq, _ = row
            key = (kanji, reading)
            if pos_field == "forms":
                if isinstance(content, list):
                    entries[key]["forms"].extend([get_text_recursive(item) for item in content])
                continue

            match = re.match(r"^(\d+)?\s*(.*)$", str(pos_field))
            s_num = int(match.group(1)) if (match and match.group(1)) else len(entries[key]["senses"]) + 1
            raw_tags = match.group(2).split() if match else []
            g_tags = sorted([t for t in raw_tags if t in CORE_POS])
            s_tags = sorted([t for t in raw_tags if t in ALLOWED_SPECIAL])
            gloss, refs = parse_structured_content(content)
            entries[key]["senses"].append({"num": s_num, "global_tags": g_tags, "special_tags": s_tags, "text": gloss, "refs": refs})
            entries[key]["score"] = max(score, entries[key]["score"])
            entries[key]["seq"] = seq

        old_format = []
        for (kanji, reading), data in entries.items():
            header = process_header(kanji, reading, data["forms"])
            content_nodes = [header, {"tag": "br"}] if header else []
            last_g_tags = None
            rules = {POS_MAPPING.get(t, t) for s in data["senses"] for t in s["global_tags"] if t in ACTIVE_RULES or t in POS_MAPPING}
            
            s_sorted = sorted(data["senses"], key=lambda x: x["num"])
            multi = len(s_sorted) > 1

            for s in s_sorted:
                if s["global_tags"] != last_g_tags:
                    tag_line = "・".join(s["global_tags"])
                    if tag_line: 
                        content_nodes.extend([f"〔{tag_line}〕", {"tag": "br"}])
                    last_g_tags = s["global_tags"]
                
                spec = f"〔{'・'.join(s['special_tags'])}〕 " if s["special_tags"] else ""
                num = f"{s['num']} " if multi else ""
                has_text = bool(s['text'])
                
                if has_text:
                    content_nodes.append(f"{num}{spec}{s['text']}")
                    if not s["refs"]: content_nodes.append({"tag": "br"})
                elif spec or num:
                    content_nodes.append(f"{num}{spec}")

                for i, r in enumerate(s["refs"]):
                    if i == 0 and has_text: content_nodes.append({"tag": "br"})
                    content_nodes.extend(["⟶ ", {"tag": "a", "href": f"?query={r}", "content": r}, {"tag": "br"}])

            if content_nodes and isinstance(content_nodes[-1], dict) and content_nodes[-1].get("tag") == "br":
                content_nodes.pop()

            old_format.append([kanji, reading, "", " ".join(sorted(list(rules))), data["score"], [{"type": "structured-content", "content": content_nodes}], data["seq"], ""])

        with open(os.path.join(output_dir, filename), 'w', encoding='utf-8') as f:
            f.write("[\n")
            for i, entry in enumerate(old_format):
                f.write(f"    {json.dumps(entry, ensure_ascii=False)}{',' if i < len(old_format)-1 else ''}\n")
            f.write("]")

def process_zip(input_zip, output_zip):
    if not os.path.exists(input_zip):
        print(f"Error: {input_zip} not found."); return

    with tempfile.TemporaryDirectory() as temp_dir:
        input_path = os.path.join(temp_dir, "extracted")
        output_path = os.path.join(temp_dir, "modified")
        os.makedirs(input_path); os.makedirs(output_path)

        with zipfile.ZipFile(input_zip, 'r') as zip_ref:
            zip_ref.extractall(input_path)

        with open(os.path.join(output_path, "index.json"), 'w', encoding='utf-8') as f:
            f.write('{"title":"JMdict","format":3,"revision":"JMdictPlaintextified","sequenced":true}')

        convert_new_to_old(input_path, output_path)

        with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_DEFLATED) as zip_out:
            for f in os.listdir(output_path):
                zip_out.write(os.path.join(output_path, f), arcname=f)
    print(f"Success: {output_zip} created.")

if __name__ == "__main__":
    target_zip = "./JMdict_english_without_proper_names.zip"
    result_zip = "./JMdictPlainText.zip"
    process_zip(target_zip, result_zip)
