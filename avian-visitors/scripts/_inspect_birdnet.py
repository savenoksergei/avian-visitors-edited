from birdnet import load, GeoPredictionSession
import inspect

print("=== load signature ===")
print(inspect.signature(load))

print("\n=== GeoPredictionSession ===")
print([m for m in dir(GeoPredictionSession) if not m.startswith('_')])

print("\n=== Try loading model ===")
try:
    model = load('acoustic', 'v2.4', 'tflite')
    print("Model loaded:", type(model))
    print("model.model_sr:", model.model_sr)
    print("model.segment_duration_s:", model.segment_duration_s)
    print("model.n_species:", model.n_species)
    print("model.species_list[:5]:", list(model.species_list)[:5])
    
    # Check GeoPredictionSession
    geo_session = GeoPredictionSession(model, 55.75, 37.62, week_48=26)
    print("\nGeo session created:", type(geo_session))
    print([m for m in dir(geo_session) if not m.startswith('_')])
    help(geo_session.run_arrays)
except Exception as e:
    import traceback
    traceback.print_exc()
