import numpy as np
from sklearn.metrics import r2_score

class TrajectorySmoothnessNode:
    def __init__(self, name="TrajectorySmoothness"):
        self.name = name

    def run(self, context):
        # 1. Ahora leemos directamente desde los resultados finales
        json_resultados = context.get("json_resultados", {})
        trayectorias = json_resultados.get("trayectorias", []) 
        
        for tray in trayectorias:
            # Soporte por si la llave se llama 'puntos' o 'puntos_px'
            raw_pts = tray.get('puntos') or tray.get('puntos_px') or tray.get('points', [])
            puntos = np.array(raw_pts)
            
            if len(puntos) < 4:
                tray['r2_score'] = 0.0 # Por defecto malo si tiene muy pocos puntos
                continue
            
            t = np.arange(len(puntos))
            x = puntos[:, 0]
            y = puntos[:, 1]
            
            # Fitteos
            r2_lin_promedio = (r2_score(x, np.poly1d(np.polyfit(t, x, 1))(t)) + 
                               r2_score(y, np.poly1d(np.polyfit(t, y, 1))(t))) / 2.0
            r2_par_promedio = (r2_score(x, np.poly1d(np.polyfit(t, x, 2))(t)) + 
                               r2_score(y, np.poly1d(np.polyfit(t, y, 2))(t))) / 2.0
            
            mejor_r2 = max(r2_lin_promedio, r2_par_promedio)
            tray['r2_score'] = float(mejor_r2)

        # 2. Guardamos las trayectorias (ahora con su r2_score) de vuelta en el JSON
        json_resultados["trayectorias"] = trayectorias
        context["json_resultados"] = json_resultados
        
        return context