import numpy as np

class TrajectorySmoothnessNode:
    def __init__(self, name="TrajectorySmoothness"):
        self.name = name

    def run(self, context):
        json_resultados = context.get("json_resultados", {})
        trayectorias = json_resultados.get("trayectorias", []) 
        
        for tray in trayectorias:
            raw_pts = tray.get('puntos') or tray.get('puntos_px') or tray.get('points', [])
            puntos = np.array(raw_pts)
            
            if len(puntos) < 4:
                tray['r2_score'] = 0.0 
                continue
            
            # 1. Usar el tiempo real 't' si existe para no deformar la velocidad
            if puntos.shape[1] >= 3:
                t = puntos[:, 2]
            else:
                t = np.arange(len(puntos))
                
            x = puntos[:, 0]
            y = puntos[:, 1]
            
            # 2. Varianza espacial total (denominador unificado)
            ss_tot = np.sum((x - np.mean(x))**2) + np.sum((y - np.mean(y))**2)
            
            if ss_tot == 0:
                tray['r2_score'] = 0.0
                continue

            # 3. FITTEO LINEAL (Grado 1 paramétrico)
            p_lin_x = np.poly1d(np.polyfit(t, x, 1))
            p_lin_y = np.poly1d(np.polyfit(t, y, 1))
            # Residuos como distancia 2D al cuadrado
            ss_res_lin = np.sum((x - p_lin_x(t))**2) + np.sum((y - p_lin_y(t))**2)
            r2_lin = 1 - (ss_res_lin / ss_tot)
            
            # 4. FITTEO PARABÓLICO (Grado 2 paramétrico)
            p_par_x = np.poly1d(np.polyfit(t, x, 2))
            p_par_y = np.poly1d(np.polyfit(t, y, 2))
            # Residuos como distancia 2D al cuadrado
            ss_res_par = np.sum((x - p_par_x(t))**2) + np.sum((y - p_par_y(t))**2)
            r2_par = 1 - (ss_res_par / ss_tot)
            
            # 5. Tomar el mejor R2 y acotar entre 0.0 y 1.0
            mejor_r2 = max(r2_lin, r2_par)
            tray['r2_score'] = float(max(0.0, mejor_r2))

        json_resultados["trayectorias"] = trayectorias
        context["json_resultados"] = json_resultados
        
        return context