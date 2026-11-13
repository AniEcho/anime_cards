from PIL import Image
import face_recognition
import os

folder_path = 'static/images/'
target_width, target_height = 500, 600
target_ratio = target_width / target_height

for filename in os.listdir(folder_path):
    if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
        img_path = os.path.join(folder_path, filename)
        try:
            img = Image.open(img_path).convert("RGB")
            width, height = img.size
            
            # Ищем лицо
            face_locations = face_recognition.face_locations(img)
            
            if face_locations:
                # Берем координаты первого найденного лица
                top, right, bottom, left = face_locations[0]
                face_center_x = (left + right) / 2
                face_center_y = (top + bottom) / 2
            else:
                # Если лицо не найдено (например, арт или маска), берем центр картинки
                face_center_x = width / 2
                face_center_y = height / 2

            # Высчитываем новые границы обрезки
            new_width = int(height * target_ratio)
            if new_width > width:
                new_width = width
                
            new_height = int(new_width / target_ratio)

            # Стараемся сделать так, чтобы лицо было по центру
            left = max(0, min(width - new_width, face_center_x - new_width / 2))
            top = max(0, min(height - new_height, face_center_y - new_height / 2))
            
            img = img.crop((left, top, left + new_width, top + new_height))
            img = img.resize((target_width, target_height), Image.Resampling.LANCZOS)
            
            img.save(img_path, quality=95)
            print(f"✅ Готово (Лицо найдено): {filename}")
        except Exception as e:
            print(f"❌ Ошибка с {filename}: {e}")