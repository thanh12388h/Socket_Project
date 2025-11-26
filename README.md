**RUN Video 360p**

**bash**
cd python_rtp

**Run Server:**

python Server.py 2026

**Run Client:**

python ClientLauncher.py 10.240.168.190 2026 5000 movie.Mjpeg 


**Run Video 720p**
**RAW MJPEG**
**EX: 720p.mjpeg cant play**
**Convert file cant play**
python convert_to_prefixed_mjpeg.py 720p.mjpeg temp.Mjpeg

**Run Server:**

python Server.py 2026

**Run Client:**

python ClientLauncher.py 10.240.168.190 2026 5000 temp.Mjpeg 

