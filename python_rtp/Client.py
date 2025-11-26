from tkinter import *
import tkinter.messagebox
from PIL import Image, ImageTk
import socket, threading, sys, traceback, os
import queue 
from time import sleep, time

from RtpPacket import RtpPacket

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
        self.frameNbr = 0
        
        # === FRAME BUFFER & REASSEMBLY ===
        self.frameBuffer = queue.Queue(maxsize=30)
        self.bufferingEvent = threading.Event()
        self.preBufferThreshold = 10
        self.isBuffering = True
        self.playbackThread = None
        
        # Fragment reassembly
        self.frames_buf = {}  # frame_id -> {'total', 'chunks', 'received', 'time'}
        self.stats_lock = threading.Lock()
        self.packets_received = 0
        self.bytes_received = 0
        
        # === FRAME RATE CONTROL ===
        self.target_fps = 30  # Default 30 FPS (adjust if needed)
        self.frame_interval = 1.0 / self.target_fps  # ~33ms per frame
        self.last_frame_time = 0
        
        # RTP listener control
        self.rtp_listener_started = False
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
            self.isBuffering = True
            self.bufferingEvent.clear()
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
            self.last_frame_time = time()  # Reset frame timing
            # Start playback thread if not running
            if self.playbackThread is None or not self.playbackThread.is_alive():
                self.playbackThread = threading.Thread(target=self.playFromBuffer, daemon=True)
                self.playbackThread.start()
            self.sendRtspRequest(self.PLAY)
            
            # Wait for pre-buffer with timeout
            if self.isBuffering:
                print(">>> Waiting for pre-buffer (timeout 5s)...")
                ready = self.bufferingEvent.wait(timeout=5.0)
                if not ready:
                    print(">>> Warning: pre-buffer timeout, continuing playback anyway")

    def listenRtp(self):
        """Listen for RTP packets, reassemble fragments, push complete frames to buffer."""
        print(">>> RTP listener started (listening on UDP port {})".format(self.rtpPort))
        self.rtp_listener_started = True
        packets_count = 0
        
        while True:
            try:
                data, addr = self.rtpSocket.recvfrom(512000)
                if not data:
                    continue
                
                packets_count += 1
                
                with self.stats_lock:
                    self.packets_received += 1
                    self.bytes_received += len(data)

                rtpPacket = RtpPacket()
                rtpPacket.decode(data)
                payload = rtpPacket.getPayload()
                rtp_seq = rtpPacket.seqNum()

                # === FRAGMENT CHECK: payload >= 8 bytes has fragment header ===
                if len(payload) >= 8:
                    try:
                        # Parse fragment header
                        frame_id = int.from_bytes(payload[0:4], 'big')
                        frag_idx = int.from_bytes(payload[4:6], 'big')
                        total = int.from_bytes(payload[6:8], 'big')
                        chunk = payload[8:]

                        if packets_count % 100 == 0:  # Log every 100 packets to reduce spam
                            print(f"RTP seq {rtp_seq}: Frame {frame_id}, frag {frag_idx}/{total} ({len(chunk)} B)")

                        # Get or create reassembly entry
                        entry = self.frames_buf.get(frame_id)
                        if not entry:
                            entry = {
                                'total': total,
                                'chunks': {},
                                'received': set(),
                                'time': time()
                            }
                            self.frames_buf[frame_id] = entry

                        # Store chunk if not received
                        if frag_idx not in entry['received']:
                            entry['chunks'][frag_idx] = chunk
                            entry['received'].add(frag_idx)

                        # Check if complete
                        if len(entry['received']) == entry['total']:
                            parts = [entry['chunks'][i] for i in range(entry['total'])]
                            frame_bytes = b''.join(parts)
                            
                            if packets_count % 1429 == 0:  # Log every complete frame (1429 fragments)
                                print(f"✓ Frame {frame_id} COMPLETE ({len(frame_bytes)} bytes)")
                            
                            # Add to buffer
                            self._addFrameToBuffer(frame_bytes)
                            
                            # Cleanup
                            try:
                                del self.frames_buf[frame_id]
                            except Exception:
                                pass

                    except Exception as e:
                        print(f"Error parsing fragment: {e}")
                        traceback.print_exc()
                        continue

                else:
                    # Legacy/small payload (< 8 bytes) -> complete frame
                    if packets_count % 100 == 0:
                        print(f"RTP seq {rtp_seq}: Legacy frame ({len(payload)} bytes)")
                    self._addFrameToBuffer(payload)

                # === CLEANUP OLD INCOMPLETE FRAMES (timeout > 2s) ===
                now = time()
                to_del = [fid for fid, e in self.frames_buf.items() if now - e['time'] > 2.0]
                for fid in to_del:
                    print(f"Cleanup: frame {fid} timeout")
                    del self.frames_buf[fid]

            except socket.timeout:
                continue
            except Exception as e:
                print(f"listenRtp exception: {e}")
                traceback.print_exc()
                if self.teardownAcked == 1:
                    break

        print(">>> RTP listener stopped")
        self.rtp_listener_started = False

    def _addFrameToBuffer(self, frame_bytes):
        """Add frame to buffer, handle overflow."""
        try:
            imageFile = self.writeFrame(frame_bytes)
            
            try:
                self.frameBuffer.put(imageFile, block=False)
                buf_size = self.frameBuffer.qsize()
                
                # Only log when buffer size changes significantly
                if buf_size % 5 == 0 or buf_size <= 2:
                    print(f">>> Buffer: {buf_size}/{self.frameBuffer.maxsize}")
                
                # Pre-buffer complete?
                if self.isBuffering and buf_size >= self.preBufferThreshold:
                    self.isBuffering = False
                    print(f">>> Pre-buffering COMPLETE! ({buf_size} frames ready)")
                    self.bufferingEvent.set()
                    
            except queue.Full:
                print(">>> Buffer FULL! Dropping oldest frame...")
                try:
                    old_file = self.frameBuffer.get_nowait()
                    try:
                        if os.path.exists(old_file):
                            os.remove(old_file)
                    except Exception:
                        pass
                    self.frameBuffer.put(imageFile, block=False)
                except queue.Empty:
                    pass
                    
        except Exception as e:
            print(f"Error adding frame to buffer: {e}")
            traceback.print_exc()

    def writeFrame(self, data):
        """Write frame to cache file."""
        cachename = CACHE_FILE_NAME + str(self.sessionId) + CACHE_FILE_EXT
        try:
            with open(cachename, "wb") as file:
                file.write(data)
        except Exception as e:
            print(f"Error writing frame: {e}")
        return cachename

    def updateMovie(self, imageFile):
        """Update GUI with frame."""
        try:
            photo = ImageTk.PhotoImage(Image.open(imageFile))
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
                        # Start playback thread if not running
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
        """Open RTP socket and start listener thread."""
        self.rtpSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtpSocket.settimeout(0.5)
        try:
            self.rtpSocket.bind(('', self.rtpPort))
            print(f"RTP socket bound on UDP port {self.rtpPort}")
            # === START LISTENER THREAD IMMEDIATELY ===
            threading.Thread(target=self.listenRtp, daemon=True).start()
        except Exception as e:
            tkinter.messagebox.showwarning('Unable to Bind', f'Unable to bind PORT={self.rtpPort}')

    def handler(self):
        """Handler for closing GUI."""
        self.pauseMovie()
        if tkinter.messagebox.askokcancel("Quit?", "Are you sure you want to quit?"):
            self.exitClient()
        else:
            self.playMovie()
            
    def playFromBuffer(self):
        """Persistent playback thread with frame rate control."""
        print(">>> Playback thread started (target FPS: {})".format(self.target_fps))
        frames_played = 0
        skipped_frames = 0
        
        while True:
            try:
                if self.state == self.PLAYING:
                    try:
                        # === GET FRAME WITH SHORT TIMEOUT ===
                        imageFile = self.frameBuffer.get(timeout=0.05)
                        
                        # === FRAME RATE CONTROL: Wait if displaying too fast ===
                        now = time()
                        elapsed = now - self.last_frame_time
                        
                        if elapsed < self.frame_interval:
                            # Display frame too fast, wait
                            wait_time = self.frame_interval - elapsed
                            sleep(wait_time)
                        
                        # Display frame
                        self.last_frame_time = time()
                        self.master.after(0, lambda f=imageFile: self.updateMovie(f))
                        frames_played += 1
                        
                        if frames_played % 30 == 0:  # Log every 30 frames
                            print(f">>> Playing: {frames_played} frames displayed, buffer: {self.frameBuffer.qsize()}, skip: {skipped_frames}")
                        
                    except queue.Empty:
                        # Buffer empty
                        if self.isBuffering:
                            if not self.rtp_listener_started:
                                print(">>> WARNING: RTP listener not running!")
                            # Don't spam log during buffering
                        else:
                            # Playing but buffer empty -> skip showing available frame
                            skipped_frames += 1
                            if skipped_frames % 10 == 0:
                                print(f">>> Waiting for buffer (skipped {skipped_frames} display cycles)")
                        sleep(0.05)
                else:
                    # Not playing - keep thread alive
                    sleep(0.1)
                    
                # Stop condition
                if self.teardownAcked == 1 and self.frameBuffer.empty():
                    print(f">>> Playback stopped. Total frames: {frames_played}, skipped: {skipped_frames}")
                    break
                    
            except Exception as e:
                print(f"playFromBuffer error: {e}")
                traceback.print_exc()
                break

        print(">>> Playback thread stopped") 