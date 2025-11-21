Run Server: 
python Server.py 1023

Create a venv environment:
py -3 -m venv venv_win

Activate a venv environment:
deactivate
.\venv_win\Scripts\Activate.ps1
pip install Pillow 

Run Client:

python ClientLauncher.py 10.126.3.140 1023 5000 movie 
