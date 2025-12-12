from tkinter import *
import tkinter.messagebox
from PIL import Image, ImageTk
import socket, threading, sys, traceback, os
from time import time, sleep

from RtpPacket import RtpPacket
from FrameBuffer import FrameBuffer  # === NEW ===

CACHE_FILE_NAME = "cache-"
CACHE_FILE_EXT = ".jpg"

class Client:
    INIT = 0
    READY = 1
    PLAYING = 2
    state = INIT

    SETUP = 0
    PLAY = 1
    PAUSE = 2
    TEARDOWN = 3

    def __init__(self, master, serveraddr, serverport, rtpport, filename):
        self.master = master
        self.master.protocol("WM_DELETE_WINDOW", self.handler)
        self.createWidgets()
        
        self.serverAddr = serveraddr
        self.serverPort = int(serverport)
        self.rtpPort = int(rtpport)
        self.fileName = filename
        self.rtspSeq = 0
        self.sessionId = 0
        self.requestSent = -1
        self.teardownAcked = 0
        self.connectToServer()
        
        # === USE FRAMEBUFFER MODULE ===
        self.frameBuffer = FrameBuffer(maxsize=30, timeout=5.0, pre_buffer=10)
        
        # Frame rate control
        self.target_fps = 30
        self.frame_interval = 1.0 / self.target_fps
        self.last_frame_time = 0
        self.playbackThread = None
        self.playEvent = threading.Event()

    def createWidgets(self):
        """Build GUI."""
        self.setup = Button(self.master, width=20, padx=3, pady=3)
        self.setup["text"] = "Setup"
        self.setup["command"] = self.setupMovie
        self.setup.grid(row=1, column=0, padx=2, pady=2)

        self.start = Button(self.master, width=20, padx=3, pady=3)
        self.start["text"] = "Play"
        self.start["command"] = self.playMovie
        self.start.grid(row=1, column=1, padx=2, pady=2)

        self.pause = Button(self.master, width=20, padx=3, pady=3)
        self.pause["text"] = "Pause"
        self.pause["command"] = self.pauseMovie
        self.pause.grid(row=1, column=2, padx=2, pady=2)

        self.teardown = Button(self.master, width=20, padx=3, pady=3)
        self.teardown["text"] = "Teardown"
        self.teardown["command"] = self.exitClient
        self.teardown.grid(row=1, column=3, padx=2, pady=2)

        self.label = Label(self.master, height=19)
        self.label.grid(row=0, column=0, columnspan=4, sticky=W+E+N+S, padx=5, pady=5)

    def setupMovie(self):
        """Setup button handler."""
        if self.state == self.INIT:
            self.frameBuffer.reset()
            print(">>> Setup: Starting to buffer frames...")
            self.sendRtspRequest(self.SETUP)

    def exitClient(self):
        """Teardown button handler."""
        if self.state != self.INIT:
            self.sendRtspRequest(self.TEARDOWN)
        self.master.destroy()
        cache_path = CACHE_FILE_NAME + str(self.sessionId) + CACHE_FILE_EXT
        try:
            if os.path.exists(cache_path):
                os.remove(cache_path)
        except Exception:
            pass

    def pauseMovie(self):
        """Pause button handler."""
        if self.state == self.PLAYING:
            self.sendRtspRequest(self.PAUSE)

    def playMovie(self):
        """Play button handler."""
        if self.state == self.READY:
            print(">>> Play: Sending PLAY request...")
            self.last_frame_time = time()
            
            if self.playbackThread is None or not self.playbackThread.is_alive():
                self.playbackThread = threading.Thread(target=self.playFromBuffer, daemon=True)
                self.playbackThread.start()
            
            self.sendRtspRequest(self.PLAY)
            
            # Wait for pre-buffer
            if self.frameBuffer.isBuffering:
                print(">>> Waiting for pre-buffer (timeout 10s)...")
                ready = self.frameBuffer.wait_pre_buffer(timeout=10.0)
                if not ready:
                    stats = self.frameBuffer.get_stats()
                    print(f">>> Pre-buffer timeout. Buffer: {stats['buffer_size']}/{stats['buffer_max']}, "
                          f"Packets: {stats['total_packets_received']}, "
                          f"Bytes: {stats['total_bytes_received']:.2f} MB")

    def listenRtp(self):
        """Listen for RTP packets and add to frame buffer."""
        print(">>> RTP listener started (UDP port {})".format(self.rtpPort))
        packets_count = 0
        
        while True:
            try:
                data, addr = self.rtpSocket.recvfrom(512000)
                if not data:
                    continue
                
                packets_count += 1
                
                # First packet debug
                if packets_count == 1:
                    print(f"✓ First RTP packet from {addr}, size: {len(data)} bytes")
                
                # Decode RTP packet
                rtpPacket = RtpPacket()
                rtpPacket.decode(data)
                payload = rtpPacket.getPayload()
                rtp_seq = rtpPacket.seqNum()
                
                # === USE FRAMEBUFFER TO PROCESS PAYLOAD ===
                is_frag, frame_id, status = self.frameBuffer.add_rtp_packet(payload, rtp_seq)
                
                # Log every 200 packets
                if packets_count % 200 == 0:
                    print(f"RTP seq {rtp_seq}: {status}")
                
                # Periodic cleanup
                if packets_count % 1000 == 0:
                    self.frameBuffer.cleanup_incomplete_frames()
                
            except socket.timeout:
                # Periodic cleanup even during timeout
                if packets_count % 1000 == 0:
                    self.frameBuffer.cleanup_incomplete_frames()
                continue
            except Exception as e:
                print(f"listenRtp exception: {e}")
                traceback.print_exc()
                if self.teardownAcked == 1:
                    break

        print(">>> RTP listener stopped")

    def writeFrame(self, data):
        """Write frame to cache file."""
        cachename = CACHE_FILE_NAME + str(self.sessionId) + CACHE_FILE_EXT
        try:
            with open(cachename, "wb") as file:
                file.write(data)
        except Exception as e:
            print(f"Error writing frame: {e}")
        return cachename

    def updateMovie(self, frame_bytes):
        """Update GUI with frame."""
        try:
            cachename = self.writeFrame(frame_bytes)
            photo = ImageTk.PhotoImage(Image.open(cachename))
            self.label.configure(image=photo, height=288)
            self.label.image = photo
        except Exception as e:
            print(f"Error updating movie: {e}")

    def connectToServer(self):
        """Connect to RTSP server."""
        self.rtspSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.rtspSocket.connect((self.serverAddr, self.serverPort))
            print(f"Connected to RTSP server {self.serverAddr}:{self.serverPort}")
        except Exception:
            tkinter.messagebox.showwarning('Connection Failed', f"Connection to '{self.serverAddr}' failed.")

    def sendRtspRequest(self, requestCode):
        """Send RTSP request."""
        request = None

        if requestCode == self.SETUP and self.state == self.INIT:
            threading.Thread(target=self.recvRtspReply, daemon=True).start()
            self.rtspSeq += 1
            request = f"SETUP {self.fileName} RTSP/1.0\r\nCSeq: {self.rtspSeq}\r\nTransport: RTP/UDP; client_port={self.rtpPort}\r\n\r\n"
            self.requestSent = self.SETUP

        elif requestCode == self.PLAY and self.state == self.READY:
            self.rtspSeq += 1
            request = f"PLAY {self.fileName} RTSP/1.0\r\nCSeq: {self.rtspSeq}\r\nSession: {self.sessionId}\r\n\r\n"
            self.requestSent = self.PLAY

        elif requestCode == self.PAUSE and self.state == self.PLAYING:
            self.rtspSeq += 1
            request = f"PAUSE {self.fileName} RTSP/1.0\r\nCSeq: {self.rtspSeq}\r\nSession: {self.sessionId}\r\n\r\n"
            self.requestSent = self.PAUSE

        elif requestCode == self.TEARDOWN and not self.state == self.INIT:
            self.rtspSeq += 1
            request = f"TEARDOWN {self.fileName} RTSP/1.0\r\nCSeq: {self.rtspSeq}\r\nSession: {self.sessionId}\r\n\r\n"
            self.requestSent = self.TEARDOWN
        else:
            return

        try:
            self.rtspSocket.send(request.encode())
        except Exception:
            tkinter.messagebox.showwarning('Send Failed', 'Failed to send RTSP request.')
            return

        print('Data sent:\n' + request)

    def recvRtspReply(self):
        """Receive RTSP reply."""
        while True:
            try:
                reply = self.rtspSocket.recv(1024)
            except Exception:
                break

            if reply:
                try:
                    text = reply.decode("utf-8", errors="replace")
                    print("RTSP Reply received:\n" + text)
                    self.parseRtspReply(text)
                except Exception:
                    traceback.print_exc()

            if self.requestSent == self.TEARDOWN:
                try:
                    self.rtspSocket.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    self.rtspSocket.close()
                except Exception:
                    pass
                break

    def parseRtspReply(self, data):
        """Parse RTSP reply."""
        lines = data.split('\n')
        if len(lines) < 3:
            return

        try:
            seqNum = int(lines[1].split(' ')[1])
        except Exception:
            return

        if seqNum == self.rtspSeq:
            try:
                session = int(lines[2].split(' ')[1])
            except Exception:
                session = 0

            if self.sessionId == 0:
                self.sessionId = session

            if self.sessionId == session:
                try:
                    code = int(lines[0].split(' ')[1])
                except Exception:
                    return

                if code == 200:
                    if self.requestSent == self.SETUP:
                        self.state = self.READY
                        self.openRtpPort()
                    elif self.requestSent == self.PLAY:
                        self.state = self.PLAYING
                        if self.playbackThread is None or not self.playbackThread.is_alive():
                            self.playbackThread = threading.Thread(target=self.playFromBuffer, daemon=True)
                            self.playbackThread.start()
                    elif self.requestSent == self.PAUSE:
                        self.state = self.READY
                        self.playEvent.set()
                    elif self.requestSent == self.TEARDOWN:
                        self.state = self.INIT
                        self.teardownAcked = 1

    def openRtpPort(self):
        """Open RTP socket and start listener."""
        self.rtpSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtpSocket.settimeout(0.5)
        try:
            self.rtpSocket.bind(('', self.rtpPort))
            print(f"RTP socket bound on UDP port {self.rtpPort}")
            threading.Thread(target=self.listenRtp, daemon=True).start()
        except Exception as e:
            tkinter.messagebox.showwarning('Unable to Bind', f'Unable to bind PORT={self.rtpPort}')

    def handler(self):
        """Handler for closing GUI."""
        self.pauseMovie()
        if tkinter.messagebox.askokcancel("Quit?", "Are you sure?"):
            self.exitClient()
        else:
            self.playMovie()

    def playFromBuffer(self):
        """Playback thread with frame rate control."""
        print(">>> Playback thread started (target FPS: {})".format(self.target_fps))
        frames_played = 0
        
        while True:
            try:
                if self.state == self.PLAYING:
                    # Get frame from buffer
                    frame_bytes = self.frameBuffer.get_frame(timeout=0.05)
                    
                    if frame_bytes:
                        # Frame rate control
                        now = time()
                        elapsed = now - self.last_frame_time
                        if elapsed < self.frame_interval:
                            sleep(self.frame_interval - elapsed)
                        
                        # Display frame
                        self.last_frame_time = time()
                        self.master.after(0, lambda f=frame_bytes: self.updateMovie(f))
                        frames_played += 1
                        
                        if frames_played % 30 == 0:
                            stats = self.frameBuffer.get_stats()
                            print(f">>> Playing: {frames_played} frames, buffer: {stats['buffer_size']}/{stats['buffer_max']}")
                else:
                    sleep(0.1)
                
                if self.teardownAcked == 1 and self.frameBuffer.frameBuffer.empty():
                    break
                    
            except Exception as e:
                print(f"playFromBuffer error: {e}")
                traceback.print_exc()
                break

        print(">>> Playback stopped")