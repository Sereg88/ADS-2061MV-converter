# -*- coding: utf-8 -*-
"""
Конвертер осциллограмм AKTAKOM / АКИП ADS-2061M  (.bin -> CSV + PNG).

Формат (вскрыт по эталону-меандру 1 кГц):
  * магия: "SPBS01e\\0"
  * заголовок ~113 байт, полезная нагрузка (отсчёты) начинается с offset 0x71
  * по умолчанию 1 байт = 1 отсчёт, 1000 отсчётов
  * по умолчанию 25 кодов = 1 деление по вертикали  ->  вольт = код / 25 * (В/дел)
    (число кодов на деление при необходимости задаётся полем "Кодов на деление")
  * значения знаковые: код 255 = -1 (всплески ниже нуля)
  * "отсчётов на деление" не вводится вручную, а считается: (Время/деление) × Частота дискретизации
  * в CSV, кроме монотонного time_ms, есть расходящийся ряд time_rel_us:
    0 в центре записи (отсчёт N/2), отрицательные до него, положительные после

Запуск GUI:      python bin2csv.py
Запуск без GUI:  python bin2csv.py <файл.bin> <В/дел> <время/дел> [отсч/дел] [смещ/дел]
                 пример: python bin2csv.py 20090511_233154.bin 2 1ms 50 0
"""

import os
import re
import sys
import zlib
import struct
import collections

PAYLOAD_OFFSET = 0x71
MAGIC = b"SPBS01e\x00"
CODES_PER_DIV = 25.0   # кодов АЦП на одно вертикальное деление

VDIV_VALUES = ["1 mV", "2 mV", "5 mV", "10 mV", "20 mV", "50 mV",
               "100 mV", "200 mV", "500 mV", "1 V", "2 V", "5 V", "10 V"]
TDIV_VALUES = ["10 ns", "20 ns", "50 ns", "100 ns", "200 ns", "500 ns",
               "1 us", "2 us", "5 us", "10 us", "20 us", "50 us",
               "100 us", "200 us", "500 us", "1 ms", "2 ms", "5 ms",
               "10 ms", "20 ms", "50 ms", "100 ms"]


# ----------------------------------------------------------------------------
# разбор значений вида "2 V", "1 ms", "500 мВ"
# ----------------------------------------------------------------------------
_UNITS = {
    "v": 1.0, "mv": 1e-3, "uv": 1e-6, "kv": 1e3,
    "в": 1.0, "мв": 1e-3, "мкв": 1e-6, "кв": 1e3,
    "s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9, "ps": 1e-12,
    "с": 1.0, "мс": 1e-3, "мкс": 1e-6, "нс": 1e-9, "пс": 1e-12,
}


def parse_scale(text):
    """'2 V' -> 2.0 ; '1 ms' -> 0.001 ; '500 мВ' -> 0.5"""
    s = str(text).strip().replace(",", ".").replace("µ", "u")
    m = re.match(r"^([0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)\s*([A-Za-zА-Яа-я]*)$", s)
    if not m:
        raise ValueError("не разобрать значение: %r" % text)
    num = float(m.group(1))
    unit = m.group(2).lower()
    if unit not in _UNITS:
        raise ValueError("неизвестная единица: %r" % m.group(2))
    return num * _UNITS[unit]


def parse_count(text):
    """Число отсчётов из строки: '1K' -> 1000 ; '10k' -> 10000 ; '1000' -> 1000 ; '' -> 0."""
    s = str(text).strip().replace(",", ".").replace(" ", "").upper()
    if not s:
        return 0
    mult = 1
    if s.endswith("K"):
        mult, s = 1000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1000000, s[:-1]
    return int(round(float(s) * mult))


def parse_rate(text):
    """Частота дискретизации в Гц: '100k' -> 1e5 ; '100 kSa/s' -> 1e5 ; '100000' -> 1e5 ; '1M' -> 1e6."""
    s = str(text).strip().replace(",", ".").replace(" ", "").lower()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*([kmg]?)", s)
    if not m:
        raise ValueError("не разобрать частоту: %r" % text)
    mult = {"": 1.0, "k": 1e3, "m": 1e6, "g": 1e9}[m.group(2)]
    return float(m.group(1)) * mult


def signed8(v):
    return v - 256 if v >= 128 else v


# ----------------------------------------------------------------------------
# чтение файла и заголовка
# ----------------------------------------------------------------------------
def read_bin(path):
    with open(path, "rb") as f:
        b = f.read()
    info = {"size": len(b), "raw": b, "magic_ok": b[:8] == MAGIC, "magic": b[:8]}
    end = b.find(b"\x00", 0x0A)
    info["model"] = b[0x0A:end].decode("ascii", "replace") if end > 0x0A else "?"
    info["channel"] = b[0x35:0x39].decode("ascii", "replace") if len(b) >= 0x39 else "?"

    def i32(o):
        return struct.unpack_from("<i", b, o)[0] if len(b) >= o + 4 else None

    def f32(o):
        return struct.unpack_from("<f", b, o)[0] if len(b) >= o + 4 else None

    info["hdr"] = {
        "0x28": b[0x28] if len(b) > 0x28 else None,
        "0x29": b[0x29] if len(b) > 0x29 else None,
        "0x38": b[0x38] if len(b) > 0x38 else None,
        "0x59": b[0x59] if len(b) > 0x59 else None,
        "i32@0x39": i32(0x39), "i32@0x3D": i32(0x3D), "i32@0x41": i32(0x41),
        "i32@0x45": i32(0x45), "samples@0x49": i32(0x49), "samples@0x4D": i32(0x4D),
        "i32@0x51": i32(0x51),
        "f32@0x61": f32(0x61), "f32@0x65": f32(0x65),
        "f32@0x69": f32(0x69), "f32@0x6D": f32(0x6D),
    }
    return info


def extract_samples(raw, offset, bps, count, signed):
    payload = raw[offset:]
    if bps == 1:
        vals = list(payload) if count <= 0 else list(payload[:count])
        if signed:
            vals = [signed8(v) for v in vals]
        return vals, "int8/sign" if signed else "uint8"
    # 2 байта на отсчёт, little-endian, знаковые 16 бит
    n = len(payload) // 2
    vals = list(struct.unpack_from("<%dh" % n, payload, 0))
    if count > 0:
        vals = vals[:count]
    if not signed:
        vals = [v & 0xFFFF for v in vals]
    return vals, "int16le"


# ----------------------------------------------------------------------------
# вывод CSV / PNG
# ----------------------------------------------------------------------------
def write_csv(path, notes, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        for l in notes:
            f.write("# " + l + "\n")
        f.write("# index;time_ms;code;volts;time_rel_us\n")
        for i, t_ms, code, volts, t_rel_us in rows:
            f.write("%d;%.4f;%d;%.6g;%.4f\n" % (i, t_ms, code, volts, t_rel_us))


def write_png(path, series, W=1000, H=340, margin=18, xgrid=50):
    lo, hi = min(series), max(series)
    pad = (hi - lo) * 0.10 or 0.5
    vmin, vmax = lo - pad, hi + pad
    px = bytearray([255] * (W * H))

    def setp(x, y, c):
        if 0 <= x < W and 0 <= y < H:
            px[y * W + x] = c

    for gy in range(5):
        y = margin + gy * (H - 1 - 2 * margin) // 4
        for x in range(W):
            setp(x, y, 210)
    for gx in range(0, W, xgrid):
        for y in range(margin, H - margin):
            setp(gx, y, 225)
    rng = (vmax - vmin) or 1.0

    def ypos(v):
        return H - 1 - margin - int((v - vmin) / rng * (H - 1 - 2 * margin))

    if vmin <= 0 <= vmax:
        for x in range(W):
            setp(x, ypos(0.0), 160)
    prev = ypos(series[0])
    for i, v in enumerate(series[:W]):
        y = ypos(v)
        for yy in range(min(prev, y), max(prev, y) + 1):
            setp(i, yy, 0)
        prev = y

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = bytearray()
    for y in range(H):
        raw.append(0)
        raw += px[y * W:(y + 1) * W]
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" +
                chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 0, 0, 0, 0)) +
                chunk(b"IDAT", zlib.compress(bytes(raw), 9)) +
                chunk(b"IEND", b""))


# ----------------------------------------------------------------------------
# основная конвертация
# ----------------------------------------------------------------------------
def default_out_base(path):
    """Папка исходного файла + <имя>_decoded (без расширения)."""
    return os.path.join(os.path.dirname(os.path.abspath(path)),
                        os.path.splitext(os.path.basename(path))[0] + "_decoded")


def unique_base(base):
    """Свободное имя: если base.csv/base.png уже есть, добавить _2, _3 ... (не перезаписываем)."""
    if not (os.path.exists(base + ".csv") or os.path.exists(base + ".png")):
        return base
    i = 2
    while os.path.exists("%s_%d.csv" % (base, i)) or os.path.exists("%s_%d.png" % (base, i)):
        i += 1
    return "%s_%d" % (base, i)


def convert(path, vdiv, tdiv, spd=50.0, offset_div=0.0, out_base=None,
            data_offset=PAYLOAD_OFFSET, bps=1, count=0, signed=True,
            probe=1.0, make_png=True, codes_per_div=CODES_PER_DIV,
            zero_index=None):
    info = read_bin(path)
    samples, kind = extract_samples(info["raw"], data_offset, bps, count, signed)
    if not samples:
        raise ValueError("не выделено ни одного отсчёта (проверьте offset/размер)")

    dt_us = (tdiv / spd) * 1e6                        # мкс/отсчёт
    volts_per_code = vdiv / codes_per_div * probe     # В на 1 код
    zero = offset_div * codes_per_div                 # смещение нуля в кодах

    n = len(samples)
    zc = n // 2 if zero_index is None else int(zero_index)   # отсчёт t = 0 (по умолчанию центр записи)

    rows = []
    for i, c in enumerate(samples):
        v = (c - zero) * volts_per_code
        rows.append((i, i * dt_us / 1000.0, c, v, (i - zc) * dt_us))
    volts = [r[3] for r in rows]

    if out_base is None:
        out_base = default_out_base(path)
    elif not os.path.dirname(out_base):
        out_base = os.path.join(os.path.dirname(os.path.abspath(path)), out_base)
    out_base = unique_base(out_base)   # существующие файлы не перезаписываем
    csv_path = out_base + ".csv"
    png_path = out_base + ".png"

    hdr = info["hdr"]
    notes = [
        "Источник: %s" % os.path.basename(path),
        "Прибор: %s, канал %s" % (info["model"], info["channel"]),
        "Отсчётов: %d (%s), смещение данных 0x%X, байт/отсчёт %d"
        % (len(samples), kind, data_offset, bps),
        "Вертикаль: %.6g В/дел, %.6g кодов/дел => %.6g В/код, смещение %.3g дел, щуп x%.6g"
        % (vdiv, codes_per_div, volts_per_code, offset_div, probe),
        "Горизонталь: %.6g с/дел, %.6g отсч/дел => dt = %.6g мкс (запись %.6g мс)"
        % (tdiv, spd, dt_us, len(samples) * dt_us / 1000.0),
        "Заголовок: %s" % ", ".join("%s=%s" % (k, hdr[k]) for k in hdr),
        "Колонки: index; time_ms (от начала записи, 0..N*dt); code (сырой код); volts (В); "
        "time_rel_us (мкс, расходящийся ряд: 0 в центре записи)",
        "Ноль времени (time_rel_us = 0): отсчёт %d из %d (центр записи)" % (zc, n),
    ]
    write_csv(csv_path, notes, rows)
    if make_png:
        write_png(png_path, volts)

    hist = collections.Counter(c for _, _, c, _, _ in rows)
    return {
        "info": info, "samples": samples, "volts": volts, "rows": rows,
        "csv": csv_path, "png": png_path if make_png else None,
        "dt_us": dt_us, "volts_per_code": volts_per_code,
        "hist": hist, "vmin": min(volts), "vmax": max(volts),
        "zero_index": zc,
    }


# ----------------------------------------------------------------------------
# GUI (форма)
# ----------------------------------------------------------------------------
def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext

    root = tk.Tk()
    root.title("ADS-2061M .bin -> CSV + PNG")
    root.geometry("1040x880")
    root.minsize(900, 780)

    v_file = tk.StringVar()
    v_vdiv = tk.StringVar(value="2 V")
    v_tdiv = tk.StringVar(value="1 ms")
    v_spd = tk.StringVar()                  # отсчётов на деление (вычисляется)
    v_fs = tk.StringVar(value="100k")       # частота дискретизации, Са/с (можно 100k, 100000)
    v_depth = tk.StringVar(value="1K")      # глубина записи, отсчётов (можно 1K, 1000)
    v_cpd = tk.StringVar(value="25")
    v_ofs = tk.StringVar(value="0")
    v_off = tk.StringVar(value=str(PAYLOAD_OFFSET))
    v_bps = tk.StringVar(value="1")
    v_sgn = tk.BooleanVar(value=True)
    v_probe = tk.StringVar(value="1")
    v_png = tk.BooleanVar(value=True)
    v_out = tk.StringVar(value="")

    def sync_out_name(*_):
        """Имя выходных файлов автоматически следует за входным."""
        p = v_file.get().strip()
        if p:
            v_out.set(default_out_base(p))

    v_file.trace_add("write", sync_out_name)

    # -- выбор файла --
    frm = ttk.LabelFrame(root, text="1. Файл осциллограммы (.bin)")
    frm.pack(fill="x", padx=8, pady=4)
    ttk.Entry(frm, textvariable=v_file).pack(side="left", fill="x", expand=True, padx=4, pady=4)

    def browse():
        p = filedialog.askopenfilename(
            title="Выберите .bin",
            filetypes=[("Осциллограмма", "*.bin"), ("Все файлы", "*.*")])
        if p:
            v_file.set(p)
            read_header_click()

    ttk.Button(frm, text="Обзор…", command=browse).pack(side="left", padx=4)

    # -- заголовок --
    hfrm = ttk.LabelFrame(root, text="Заголовок файла (для проверки)")
    hfrm.pack(fill="both", padx=8, pady=4)
    txt = scrolledtext.ScrolledText(hfrm, height=9, font=("Consolas", 9))
    txt.pack(fill="both", expand=True, padx=4, pady=4)

    def read_header_click():
        p = v_file.get().strip()
        if not p or not os.path.exists(p):
            messagebox.showwarning("Нет файла", "Укажите существующий .bin")
            return
        info = read_bin(p)
        txt.delete("1.0", "end")
        txt.insert("end", "файл: %s\n" % p)
        txt.insert("end", "размер: %d байт | магия: %s (%s)\n"
                   % (info["size"], info["magic"], "OK" if info["magic_ok"] else "НЕ совпала"))
        txt.insert("end", "модель: %s | канал: %s\n" % (info["model"], info["channel"]))
        for k, val in info["hdr"].items():
            txt.insert("end", "  %-14s = %s\n" % (k, val))
        pl = info["raw"][PAYLOAD_OFFSET:]
        hist = collections.Counter(pl)
        txt.insert("end", "payload: %d байт, уникальных значений %d, min=%d max=%d\n"
                   % (len(pl), len(hist), min(pl) if pl else 0, max(pl) if pl else 0))
        txt.insert("end", "топ-10 кодов: %s\n"
                   % ", ".join("%d×%d" % (v, n) for v, n in hist.most_common(10)))
        v_out.set(default_out_base(p))

    ttk.Button(hfrm, text="Прочитать заголовок", command=read_header_click)\
        .pack(anchor="w", padx=4, pady=(0, 4))

    # -- калибровка --
    cfrm = ttk.LabelFrame(root, text="2. Калибровка (введите как на экране прибора)")
    cfrm.pack(fill="x", padx=8, pady=4)

    def row(parent, label, widget, r):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", padx=4, pady=3)
        widget.grid(row=r, column=1, sticky="w", padx=4, pady=3)

    cb_v = ttk.Combobox(cfrm, textvariable=v_vdiv, values=VDIV_VALUES, width=12)
    row(cfrm, "Вольт/деление:", cb_v, 0)
    cb_t = ttk.Combobox(cfrm, textvariable=v_tdiv, values=TDIV_VALUES, width=12)
    row(cfrm, "Время/деление:", cb_t, 1)
    row(cfrm, "Частота дискретизации (Са/с):", ttk.Entry(cfrm, textvariable=v_fs, width=12), 2)
    row(cfrm, "Глубина записи (отсчётов):", ttk.Entry(cfrm, textvariable=v_depth, width=12), 3)
    ttk.Label(cfrm, text="Отсчётов на деление:").grid(row=4, column=0, sticky="w", padx=4, pady=3)
    ttk.Label(cfrm, textvariable=v_spd, foreground="#555").grid(row=4, column=1, sticky="w", padx=4, pady=3)
    row(cfrm, "Кодов на деление:", ttk.Entry(cfrm, textvariable=v_cpd, width=12), 5)
    row(cfrm, "Смещение, делений (офсет):", ttk.Entry(cfrm, textvariable=v_ofs, width=12), 6)
    row(cfrm, "Множитель щупа (x):", ttk.Entry(cfrm, textvariable=v_probe, width=12), 7)

    hint = ttk.Label(cfrm, foreground="#555", text=(
        "Отсчётов на деление считается само: (Время/деление) × (Частота дискретизации).   "
        "Пример: 500 us × 100k = 50 отсч/дел.\n"
        "Частота: пишите 100k (=100 кСа/с) или 100000.  Глубина: 1K = 1000 отсчётов.\n"
        "volts = (код - офсет*кодов_на_дел) / кодов_на_дел * (В/дел) * щуп"))
    hint.grid(row=8, column=0, columnspan=2, sticky="w", padx=4, pady=(6, 2))

    def sync_spd(*_):
        """Показывает вычисленные отсчёты на деление по мере ввода."""
        try:
            tdiv = parse_scale(v_tdiv.get())
            fs = parse_rate(v_fs.get())
            v_spd.set("%.6g" % (tdiv * fs))
        except Exception:
            v_spd.set("—")
    for _v in (v_tdiv, v_fs):
        _v.trace_add("write", sync_spd)
    sync_spd()

    # -- дополнительные параметры формата --
    afrm = ttk.LabelFrame(root, text="3. Формат данных (нужно, если файл отличается)")
    afrm.pack(fill="x", padx=8, pady=4)
    afrm.columnconfigure(5, weight=1)
    row(afrm, "Смещение данных (hex/dec):", ttk.Entry(afrm, textvariable=v_off, width=10), 0)
    ttk.Label(afrm, text="Байт/отсчёт:").grid(row=0, column=2, sticky="w", padx=6)
    ttk.Combobox(afrm, textvariable=v_bps, values=["1", "2"], width=4)\
        .grid(row=0, column=3, sticky="w")
    ttk.Checkbutton(afrm, text="знаковый", variable=v_sgn).grid(row=0, column=4, sticky="w", padx=6)
    ttk.Checkbutton(afrm, text="строить PNG", variable=v_png).grid(row=1, column=0, columnspan=2,
                                                                   sticky="w", padx=4, pady=(4, 2))
    ttk.Label(afrm, text="Имя для выходных файлов (без расширения):")\
        .grid(row=2, column=0, columnspan=2, sticky="w", padx=4, pady=(4, 2))
    ttk.Entry(afrm, textvariable=v_out).grid(row=3, column=0, columnspan=7, sticky="we",
                                             padx=4, pady=(0, 6))

    # -- кнопка --
    bfrm = ttk.Frame(root)
    bfrm.pack(fill="x", padx=8, pady=4)

    def do_convert():
        p = v_file.get().strip()
        if not p or not os.path.exists(p):
            messagebox.showwarning("Нет файла", "Укажите существующий .bin")
            return
        make_png = bool(v_png.get())
        base = v_out.get().strip() or default_out_base(p)
        if not os.path.dirname(base):
            base = os.path.join(os.path.dirname(os.path.abspath(p)), base)
        try:
            off = int(str(v_off.get()).strip(), 0)
            tdiv = parse_scale(v_tdiv.get())
            spd = tdiv * parse_rate(v_fs.get())          # отсчётов на деление = Время/дел × Fs
            depth = parse_count(v_depth.get())           # глубина записи, отсчётов
            res = convert(
                p,
                parse_scale(v_vdiv.get()), tdiv, spd,
                float(str(v_ofs.get()).replace(",", ".")),
                out_base=base,
                data_offset=off, bps=int(v_bps.get()),
                count=depth, signed=bool(v_sgn.get()),
                probe=float(str(v_probe.get()).replace(",", ".")),
                codes_per_div=float(str(v_cpd.get()).replace(",", ".")),
                make_png=make_png)
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))
            return
        txt.insert("end", "\n--- ГОТОВО ---\n")
        txt.insert("end", "отсчётов на деление: %.6g (Время/дел %.6g с × Fs %s)\n"
                   % (spd, tdiv, v_fs.get()))
        used = os.path.splitext(res["csv"])[0]
        if os.path.normcase(used) != os.path.normcase(base):
            txt.insert("end", "имя %s уже было занято -> сохранено как %s\n"
                       % (os.path.basename(base), os.path.basename(used)))
        txt.insert("end", "dt = %.6g мкс/отсчёт; %.6g В/код\n"
                   % (res["dt_us"], res["volts_per_code"]))
        txt.insert("end", "отсчётов: %d; диапазон напряжений: %.6g .. %.6g В\n"
                   % (len(res["samples"]), res["vmin"], res["vmax"]))
        txt.insert("end", "топ-10 кодов: %s\n"
                   % ", ".join("%d×%d" % (v, n) for v, n in res["hist"].most_common(10)))
        txt.insert("end", "первые 12 значений: %s\n"
                   % ", ".join("%.4g" % x for x in res["volts"][:12]))
        txt.insert("end", "ноль времени (time_rel_us=0): отсчёт %d из %d\n"
                   % (res["zero_index"], len(res["samples"])))
        txt.insert("end", "CSV: %s\n" % res["csv"])
        if res["png"]:
            txt.insert("end", "PNG: %s\n" % res["png"])
        txt.see("end")

    ttk.Button(bfrm, text="Декодировать в CSV + PNG", command=do_convert)\
        .pack(side="left", padx=4)
    ttk.Label(bfrm, foreground="#555", wraplength=980, justify="left",
              text="Совет: если коды «схлопнуты» в 1-2 значения — увеличьте чувствительность (меньше В/дел) на приборе.")\
        .pack(side="left", padx=10)

    root.mainloop()


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def run_cli(argv):
    path = argv[0]
    vdiv = parse_scale(argv[1])
    tdiv = parse_scale(argv[2])
    spd = float(argv[3]) if len(argv) > 3 else 50.0
    ofs = float(argv[4]) if len(argv) > 4 else 0.0
    res = convert(path, vdiv, tdiv, spd, ofs)
    print("dt = %.6g мкс/отсчёт; %.6g В/код" % (res["dt_us"], res["volts_per_code"]))
    print("диапазон: %.6g .. %.6g В" % (res["vmin"], res["vmax"]))
    print("топ кодов:", ", ".join("%d×%d" % (v, n) for v, n in res["hist"].most_common(10)))
    print("CSV:", res["csv"])
    if res["png"]:
        print("PNG:", res["png"])


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_cli(sys.argv[1:])
    else:
        run_gui()
