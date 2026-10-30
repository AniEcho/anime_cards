import cv2
import numpy as np
import re
from pathlib import Path

PUZZLE_DIR = Path(r'C:\anime_cards\static\images\puzzle')

MASK_PATTERN = re.compile(r'^mask_(small|big)_(\d+)\.jpg$', re.IGNORECASE)

def cut_mask(mask_path, out_dir, expected_count):
    print(f"Обрабатываю: {mask_path.name}")
    img = cv2.imread(str(mask_path))
    if img is None:
        print(f"ОШИБКА: не удалось загрузить {mask_path}")
        return

    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # 1. Белые куски
    white_mask = cv2.inRange(hsv, np.array([0, 0, 170]), np.array([180, 80, 255]))

    # 2. Исключаем зеленый фон
    green_mask = cv2.inRange(hsv, np.array([30, 30, 30]), np.array([100, 255, 255]))
    white_mask = cv2.bitwise_and(white_mask, cv2.bitwise_not(green_mask))

    # 3. Черные линии (утолщаем 3x3, чтобы точно разделить)
    black_mask = cv2.inRange(gray, 0, 100)
    line_kernel = np.ones((3, 3), np.uint8)
    black_mask = cv2.dilate(black_mask, line_kernel, iterations=1)

    # 4. Убираем линии из кусков
    pieces = cv2.bitwise_and(white_mask, cv2.bitwise_not(black_mask))

    # 5. БЕЗ МОРФОЛОГИИ! (только если совсем мелкий шум, но лучше без нее)
    # pieces = cv2.morphologyEx(pieces, cv2.MORPH_OPEN, clean_kernel)
    # pieces = cv2.morphologyEx(pieces, cv2.MORPH_CLOSE, clean_kernel)

    # --- 6. DEBUG ---
    debug_dir = PUZZLE_DIR / "_debug"
    debug_dir.mkdir(exist_ok=True)
    cv2.imwrite(str(debug_dir / f"{mask_path.stem}_pieces.png"), pieces)
    cv2.imwrite(str(debug_dir / f"{mask_path.stem}_black.png"), black_mask)

    # 7. Ищем области
    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(pieces, connectivity=8)

    valid = []
    for label in range(1, num_labels):
        area = stats[label, cv2.CC_STAT_AREA]
        if area < 2000: continue # Чуть увеличил порог
        valid.append({
            "label": label,
            "area": area,
            "x": stats[label, cv2.CC_STAT_LEFT],
            "y": stats[label, cv2.CC_STAT_TOP],
            "w": stats[label, cv2.CC_STAT_WIDTH],
            "h": stats[label, cv2.CC_STAT_HEIGHT]
        })

    valid.sort(key=lambda p: p["area"], reverse=True)
    print(f"Найдено кусков: {len(valid)}, нужно: {expected_count}")

    if len(valid) < expected_count:
        print("ВНИМАНИЕ: найдено меньше кусков, чем ожидалось.")

    valid = valid[:expected_count]
    if not valid: return

    out_dir.mkdir(parents=True, exist_ok=True)

    for i, piece in enumerate(valid):
        label = piece["label"]
        
        # Точная маска куска (без расширения)
        piece_mask = (labels == label).astype(np.uint8) * 255
        
        # Используем оригинальную картинку
        rgba = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
        rgba[:, :, 3] = piece_mask

        # Плотная обрезка
        y_indices, x_indices = np.where(piece_mask > 0)
        if len(y_indices) == 0: continue

        x_min = max(0, x_indices.min())
        x_max = min(img.shape[1], x_indices.max() + 1)
        y_min = max(0, y_indices.min())
        y_max = min(img.shape[0], y_indices.max() + 1)

        cropped = rgba[y_min:y_max, x_min:x_max]
        cv2.imwrite(str(out_dir / f"piece_{i}.png"), cropped)

    print(f"Сохранено {len(valid)} кусков в {out_dir}")


if __name__ == "__main__":
    if not PUZZLE_DIR.exists(): exit()

    mask_files = list(PUZZLE_DIR.glob("mask_*.jpg"))
    if not mask_files:
        print("Файлы масок не найдены.")
        exit()

    for mask_path in mask_files:
        match = MASK_PATTERN.match(mask_path.name)
        if not match:
            print(f"Пропускаю файл с неподходящим именем: {mask_path.name}")
            continue
        
        type_prefix = match.group(1)
        variant = match.group(2)
        
        if type_prefix == 'small':
            expected = 8
        else:
            expected = 14

        out_dir = PUZZLE_DIR / f"{type_prefix}_{variant}"
        cut_mask(mask_path, out_dir, expected)

    print("Готово!")