import fiona

def list_layers(gpkg_path):
    return fiona.listlayers(gpkg_path)


def inspect_layer(gpkg_path, layer_name):
    with fiona.open(gpkg_path, layer=layer_name) as src:
        return {
            "feature_count": len(src),
            "geometry_type": src.schema["geometry"].upper(),
            "crs": src.crs,
            "crs_epsg": src.crs.get("init", "").replace("epsg:", "")
        }
