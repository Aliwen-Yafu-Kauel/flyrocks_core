import numpy as np

SQLITE_URL = "sqlite:///./flyrocks.db"

class Config:
    def __init__(self, video_path: str, origin_zone: list, projection_zone: list, h_matrix: list): #, detonation_csv_path: str):
        self.VIDEO_PATH = video_path
        self.STAB_SCALE = 0.4  

        # Parámetros base
        self.MIN_FRAMES_TO_CONFIRM = 5  
        self.MIN_AREA = 15        
        self.MAX_AREA = 2500    
        self.DIST_THRESH = 350  
        self.MAX_MISSING = 40 
        self.MIN_SOLIDITY = 0.70  
        self.EDGE_MARGIN = 5
        self.MAX_FALL_RATIO = 3.0
        
        # self.detonation_csv_path = detonation_csv_path

        # Física
        self.MIN_DISPLACEMENT_BASE = 50  
        self.MAX_TORTUOSITY = 1.3  
        self.GRAVEDAD_PIXELS = 1.2 

        # Convertimos las listas recibidas de la API a los formatos numpy requeridos
        self.ORIGIN_ZONE = np.array(origin_zone, dtype=np.int32).reshape((-1, 1, 2))
        self.EXPECTED_PROJECTION_ZONE = np.array(projection_zone, dtype=np.int32).reshape((-1, 1, 2))
        self.H_MATRIX = np.array(h_matrix, dtype=np.float64).reshape(3, 3)
        
