# -*- coding: utf-8 -*-
"""
Конвертер осциллограмм AKTAKOM / АКИП ADS-2061M  (.bin -> CSV + PNG).

Формат (вскрыт по эталону-меандру 5 В / 1 кГц, 2 В/дел, 1 мс/дел):
  * магия: "SPBS01e\\0"
  * заголовок ~113 байт, полезная нагрузка (отсчёты) начинается с offset 0x71
  * по умолчанию 1 байт = 1 отсчёт, 1000 отсчётов
  * 1 код = 0.1 деления по вертикали  ->  вольт = код * 0.1 * (В/дел)
  * значения знаковые: код 255 = -1 (всплески ниже нуля)

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
        f.write("# index;time_ms;code;volts\n")
        for i, t_ms, code, volts in rows:
            f.write("%d;%.4f;%d;%.6g\n" % (i, t_ms, code, volts))


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
            probe=1.0, make_png=True):
    info = read_bin(path)
    samples, kind = extract_samples(info["raw"], data_offset, bps, count, signed)
    if not samples:
        raise ValueError("не выделено ни одного отсчёта (проверьте offset/размер)")

    dt_us = (tdiv / spd) * 1e6              # мкс/отсчёт
    volts_per_code = 0.1 * vdiv * probe     # 1 код = 0.1 деления
    zero = offset_div * 10.0                # смещение нуля в кодах

    rows = []
    for i, c in enumerate(samples):
        v = (c - zero) * volts_per_code
        rows.append((i, i * dt_us / 1000.0, c, v))
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
        "Вертикаль: %.6g В/дел, 1 код = 0.1 дел => %.6g В/код, смещение %.3g дел, щуп x%.6g"
        % (vdiv, volts_per_code, offset_div, probe),
        "Горизонталь: %.6g с/дел, %.6g отсч/дел => dt = %.6g мкс (запись %.6g мс)"
        % (tdiv, spd, dt_us, len(samples) * dt_us / 1000.0),
        "Заголовок: %s" % ", ".join("%s=%s" % (k, hdr[k]) for k in hdr),
        "Колонки: index; time_ms; code (сырой код); volts (В)",
    ]
    write_csv(csv_path, notes, rows)
    if make_png:
        write_png(png_path, volts)

    hist = collections.Counter(c for _, _, c, _ in rows)
    return {
        "info": info, "samples": samples, "volts": volts, "rows": rows,
        "csv": csv_path, "png": png_path if make_png else None,
        "dt_us": dt_us, "volts_per_code": volts_per_code,
        "hist": hist, "vmin": min(volts), "vmax": max(volts),
    }


# ----------------------------------------------------------------------------
# GUI (форма)
# ----------------------------------------------------------------------------
def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext

    root = tk.Tk()
    root.title("ADS-2061M .bin -> CSV + PNG")
    root.geometry("860x640")

    v_file = tk.StringVar()
    v_vdiv = tk.StringVar(value="2 V")
    v_tdiv = tk.StringVar(value="1 ms")
    v_spd = tk.StringVar(value="50")
    v_ofs = tk.StringVar(value="0")
    v_off = tk.StringVar(value=str(PAYLOAD_OFFSET))
    v_bps = tk.StringVar(value="1")
    v_cnt = tk.StringVar(value="0")
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
    row(cfrm, "Отсчётов на деление:", ttk.Entry(cfrm, textvariable=v_spd, width=12), 2)
    row(cfrm, "Смещение, делений (офсет):", ttk.Entry(cfrm, textvariable=v_ofs, width=12), 3)
    row(cfrm, "Множитель щупа (x):", ttk.Entry(cfrm, textvariable=v_probe, width=12), 4)

    hint = ttk.Label(cfrm, foreground="#555", text=(
        "dt = (Время/деление) / (отсчётов на деление).   "
        "Для эталона: 1 ms / 50 = 20 мкс/отсчёт.\n"
        "volts = (код - смещение*10) * 0.1 * (В/дел) * щуп"))
    hint.grid(row=5, column=0, columnspan=2, sticky="w", padx=4, pady=(6, 2))

    # -- дополнительные параметры формата --
    afrm = ttk.LabelFrame(root, text="3. Формат данных (нужно, если файл отличается)")
    afrm.pack(fill="x", padx=8, pady=4)
    afrm.columnconfigure(5, weight=1)
    row(afrm, "Смещение данных (hex/dec):", ttk.Entry(afrm, textvariable=v_off, width=10), 0)
    ttk.Label(afrm, text="Байт/отсчёт:").grid(row=0, column=2, sticky="w", padx=6)
    ttk.Combobox(afrm, textvariable=v_bps, values=["1", "2"], width=4)\
        .grid(row=0, column=3, sticky="w")
    ttk.Label(afrm, text="Отсчётов (0 = авто):").grid(row=0, column=4, sticky="w", padx=6)
    ttk.Entry(afrm, textvariable=v_cnt, width=8).grid(row=0, column=5, sticky="w")
    ttk.Checkbutton(afrm, text="знаковый", variable=v_sgn).grid(row=0, column=6, sticky="w", padx=6)
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
            res = convert(
                p,
                parse_scale(v_vdiv.get()), parse_scale(v_tdiv.get()),
                float(str(v_spd.get()).replace(",", ".")),
                float(str(v_ofs.get()).replace(",", ".")),
                out_base=base,
                data_offset=off, bps=int(v_bps.get()),
                count=int(v_cnt.get() or 0), signed=bool(v_sgn.get()),
                probe=float(str(v_probe.get()).replace(",", ".")),
                make_png=make_png)
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))
            return
        txt.insert("end", "\n--- ГОТОВО ---\n")
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
        txt.insert("end", "CSV: %s\n" % res["csv"])
        if res["png"]:
            txt.insert("end", "PNG: %s\n" % res["png"])
        txt.see("end")

    ttk.Button(bfrm, text="Декодировать в CSV + PNG", command=do_convert)\
        .pack(side="left", padx=4)
    ttk.Label(bfrm, foreground="#555",
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
