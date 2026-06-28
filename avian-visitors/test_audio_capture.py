"""
test_audio_capture.py — Тест Части 2 без микрофона.

Три уровня тестирования:
  1. Загрузка модели BirdNET + структура результата
  2. Inference на тишине и шуме
  3. Целостный тест _process_segment → Database

Запуск:  python3 test_audio_capture.py
"""

import os
import sys
import time
import logging
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.WARNING,  # меньше шума от TF
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  — {detail}")


def test_1_model_loading():
    """Тест 1: Загрузка BirdNET модели и проверка её свойств."""
    print("\n═══ Тест 1: Загрузка BirdNET модели ═══")

    from birdnet import load as birdnet_load

    t0 = time.time()
    model = birdnet_load("acoustic", "2.4", "tf")
    t1 = time.time()
    print(f"  Модель загружена за {t1 - t0:.1f}с (кэш)")

    check("get_sample_rate() == 48000", model.get_sample_rate() == 48000,
          f"got {model.get_sample_rate()}")
    check("get_segment_size_s() == 3.0", model.get_segment_size_s() == 3.0,
          f"got {model.get_segment_size_s()}")
    check("get_segment_size_samples() == 144000", model.get_segment_size_samples() == 144000,
          f"got {model.get_segment_size_samples()}")
    check("n_species > 6000", model.n_species > 6000, f"got {model.n_species}")
    check("species_list не пустой", len(model.species_list) > 0)

    first5 = list(model.species_list)[:5]
    print(f"  Примеры видов: {first5}")

    return model


def test_2_inference_on_silence(model):
    """Тест 2: Inference на тишине — не должно быть детекций > 0.25."""
    print("\n═══ Тест 2: Inference на тишине ═══")

    silence = np.zeros(48000 * 3, dtype=np.float32)
    t0 = time.time()
    result = model.predict_arrays(
        (silence, 48000),
        top_k=None,
        default_confidence_threshold=0.0,
    )
    t1 = time.time()
    print(f"  Inference за {t1 - t0:.2f}с")

    check("species_probs shape == (1,1,N)", result.species_probs.shape[0] == 1
          and result.species_probs.shape[1] == 1,
          f"got {result.species_probs.shape}")
    check("species_list len == n_species", len(result.species_list) > 6000)

    probs = result.species_probs[0, 0]
    max_conf = float(np.max(probs))
    print(f"  max confidence на тишине: {max_conf:.6f}")
    check("Тишина: max_conf < 0.1", max_conf < 0.1, f"got {max_conf:.4f}")

    above_001 = int(np.sum(probs > 0.01))
    print(f"  Видов с conf > 0.01: {above_001}")

    return result


def test_3_inference_on_noise(model):
    """Тест 3: Inference на белом шуме — проверяем что код не падает."""
    print("\n═══ Тест 3: Inference на белом шуме ═══")

    rng = np.random.default_rng(42)
    noise = rng.normal(0, 0.1, size=48000 * 3).astype(np.float32)
    t0 = time.time()
    result = model.predict_arrays(
        (noise, 48000),
        top_k=None,
        default_confidence_threshold=0.0,
    )
    t1 = time.time()
    print(f"  Inference за {t1 - t0:.2f}с")

    probs = result.species_probs[0, 0]
    max_conf = float(np.max(probs))
    above_25 = int(np.sum(probs > 0.25))
    above_01 = int(np.sum(probs > 0.01))
    print(f"  max confidence: {max_conf:.6f}")
    print(f"  Видов с conf > 0.25: {above_25}")
    print(f"  Видов с conf > 0.01: {above_01}")

    top5_idx = np.argsort(probs)[-5:][::-1]
    print("  Топ-5 на шуме:")
    for idx in top5_idx:
        print(f"    {probs[idx]:.4f}  {result.species_list[idx]}")

    return result


def test_4_process_segment_to_db():
    """Тест 4: _process_segment() → запись в БД."""
    print("\n═══ Тест 4: _process_segment → Database ═══")

    from database import Database
    from audio_capture import AudioListener

    test_db = "/tmp/test_audio_capture.db"
    if os.path.exists(test_db):
        os.remove(test_db)

    db = Database(db_path=test_db)
    db.init()

    listener = AudioListener(db, confidence_threshold=0.25)

    # 4a: Тишина — не должно быть записей
    silence = np.zeros(48000 * 3, dtype=np.float32)
    t0 = time.time()
    listener._process_segment(silence)
    t1 = time.time()
    print(f"  Тишина обработана за {t1 - t0:.2f}с")

    check("segments_processed >= 1", listener._segments_processed >= 1)
    stats = db.stats()
    check("Тишина: 0 детекций", stats["totals"]["detections"] == 0,
          f"got {stats['totals']['detections']}")

    # 4b: Белый шум — может быть детекции (шум может триггерить модель)
    rng = np.random.default_rng(42)
    noise = rng.normal(0, 0.1, size=48000 * 3).astype(np.float32)
    listener._process_segment(noise)

    stats2 = db.stats()
    print(f"  После шума: {stats2['totals']['detections']} детекций, "
          f"{stats2['totals']['species']} видов")

    if stats2["totals"]["detections"] > 0:
        check("Шум дал детекции (возможно)", True)
        # Проверяем структуру записей
        ll = db.lifelist()
        sp = ll["species"][0]
        check("lifelist[0] имеет 'sci'", "sci" in sp)
        check("lifelist[0] имеет 'com'", "com" in sp)
        check("lifelist[0] имеет 'first_seen'", "first_seen" in sp)
        check("lifelist[0] имеет 'best_conf'", "best_conf" in sp)
        check("best_conf > 0.25", sp["best_conf"] > 0.25,
              f"got {sp['best_conf']:.4f}")
        # Проверяем, что sci_name и com_name корректно разделены
        check("sci_name не содержит '_'", "_" not in sp["sci"],
              f"got '{sp['sci']}'")
        check("com_name ≠ sci_name", sp["com"] != sp["sci"],
              f"com='{sp['com']}', sci='{sp['sci']}'")
        print(f"  Топ вид: {sp['sci']} / {sp['com']} (conf={sp['best_conf']:.3f})")
    else:
        print("  ⚠️  Шум не дал детекций > 0.25 — это нормально")

    # 4c: Модель загружена лениво
    check("Модель загружена", listener._model is not None)

    db.close()
    os.remove(test_db)


def test_5_lifecycle():
    """Тест 5: API AudioListener без микрофона."""
    print("\n═══ Тест 5: Жизненный цикл ═══")

    from database import Database
    from audio_capture import AudioListener

    test_db = "/tmp/test_lifecycle.db"
    if os.path.exists(test_db):
        os.remove(test_db)

    db = Database(db_path=test_db)
    db.init()
    listener = AudioListener(db)

    check("is_running == False", not listener.is_running)
    check("stats: segments == 0", listener.stats["segments_processed"] == 0)
    check("stats: detections == 0", listener.stats["detections_written"] == 0)

    # stop() на не запущенном — не должен крашиться
    listener.stop()
    check("stop() на не запущенном — без ошибки", True)

    db.close()
    os.remove(test_db)


def main():
    global PASS, FAIL
    print("=" * 60)
    print("  Тестирование audio_capture.py (Часть 2)")
    print("=" * 60)

    model = None
    try:
        model = test_1_model_loading()
        test_2_inference_on_silence(model)
        test_3_inference_on_noise(model)
        test_4_process_segment_to_db()
        test_5_lifecycle()
    except Exception as e:
        import traceback
        print(f"\n💥 ОШИБКА: {e}")
        traceback.print_exc()
        FAIL += 1  # noqa: FAIL is global

    print("\n" + "=" * 60)
    print(f"  Результат: {PASS} ✅  {FAIL} ❌")
    print("=" * 60)

    if FAIL > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()