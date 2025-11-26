from tkinter import *
import tkinter.messagebox
from PIL import Image, ImageTk
import socket, threading, sys, traceback, os

from RtpPacket import RtpPacket
import heapq, time

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

    # Initiation..
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
        # buffer for fragmented frames: frame_id -> { 'total':int, 'chunks':{}, 'received':set(), 'time':float }
        self.frames_buf = {}
        # stats
        self.packets_received = 0
        self.bytes_received = 0
        # playback queue (min-heap) of (timestamp_ms, frame_bytes)
        self.play_queue = []
        self.play_lock = threading.Lock()
        self.play_start_time_ms = None
        self.play_start_ts = None
        # playback/jitter settings
        self.jitter_ms = int(os.getenv('RTP_JITTER_MS', '200'))
        # stats
        self.frames_reassembled = 0
        self.frames_displayed = 0
        self.frames_dropped = 0

    def createWidgets(self):
        """Build GUI."""
        # Create Setup button
        self.setup = Button(self.master, width=20, padx=3, pady=3)
        self.setup["text"] = "Setup"
        self.setup["command"] = self.setupMovie
        self.setup.grid(row=1, column=0, padx=2, pady=2)

        # Create Play button		
        self.start = Button(self.master, width=20, padx=3, pady=3)
        self.start["text"] = "Play"
        self.start["command"] = self.playMovie
        self.start.grid(row=1, column=1, padx=2, pady=2)

        # Create Pause button			
        self.pause = Button(self.master, width=20, padx=3, pady=3)
        self.pause["text"] = "Pause"
        self.pause["command"] = self.pauseMovie
        self.pause.grid(row=1, column=2, padx=2, pady=2)

        # Create Teardown button
        self.teardown = Button(self.master, width=20, padx=3, pady=3)
        self.teardown["text"] = "Teardown"
        self.teardown["command"] =  self.exitClient
        self.teardown.grid(row=1, column=3, padx=2, pady=2)

        # Create a label to display the movie
        self.label = Label(self.master, height=19)
        self.label.grid(row=0, column=0, columnspan=4, sticky=W+E+N+S, padx=5, pady=5)

    def setupMovie(self):
        """Setup button handler."""
        if self.state == self.INIT:
            self.sendRtspRequest(self.SETUP)

    def exitClient(self):
        """Teardown button handler."""
        self.sendRtspRequest(self.TEARDOWN)
        # Close the gui window
        self.master.destroy()
        # Delete the cache image from video (if exists)
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
            # Create playEvent first so threads can observe it
            self.playEvent = threading.Event()
            self.playEvent.clear()
            # Create threads
            threading.Thread(target=self.listenRtp, daemon=True).start()
            threading.Thread(target=self.playbackThread, daemon=True).start()
            self.sendRtspRequest(self.PLAY)

    def listenRtp(self):
        """Listen for RTP packets."""
        while True:
            try:
                data = self.rtpSocket.recv(65536)
                if data:
                    self.packets_received += 1
                    self.bytes_received += len(data)

                    rtpPacket = RtpPacket()
                    rtpPacket.decode(data)

                    payload = rtpPacket.getPayload()
                    # extract timestamp (ms) for playout
                    try:
                        pkt_ts = rtpPacket.timestamp()
                    except Exception:
                        pkt_ts = int(time.time() * 1000)

                    # expect custom fragment header: 4B frame_id,2B frag_idx,2B total
                    if len(payload) < 8:
                        # malformed or legacy packet: treat entire payload as one frame
                        currFrameNbr = rtpPacket.seqNum()
                        print("Current Seq Num: " + str(currFrameNbr))
                        if currFrameNbr > self.frameNbr:
                            self.frameNbr = currFrameNbr
                            self.updateMovie(self.writeFrame(payload))
                        continue

                    frame_id = int.from_bytes(payload[0:4], 'big')
                    frag_idx = int.from_bytes(payload[4:6], 'big')
                    total = int.from_bytes(payload[6:8], 'big')
                    chunk = payload[8:]

                    entry = self.frames_buf.get(frame_id)
                    if not entry:
                        entry = {'total': total, 'chunks': {}, 'received': set(), 'time': __import__('time').time(), 'timestamp': pkt_ts}
                        self.frames_buf[frame_id] = entry

                    # store chunk
                    if frag_idx not in entry['received']:
                        entry['chunks'][frag_idx] = chunk
                        entry['received'].add(frag_idx)

                    # if complete -> reassemble
                    if len(entry['received']) == entry['total']:
                        parts = [entry['chunks'][i] for i in range(entry['total'])]
                        frame_bytes = b''.join(parts)
                        # update frame number and enqueue for playout
                        self.frames_reassembled += 1
                        print("Reassembled Frame ID: %d ts=%d" % (frame_id, entry.get('timestamp', 0)))
                        with self.play_lock:
                            heapq.heappush(self.play_queue, (entry.get('timestamp', pkt_ts), frame_bytes))
                        if frame_id > self.frameNbr:
                            self.frameNbr = frame_id
                        # cleanup
                        try:
                            del self.frames_buf[frame_id]
                        except Exception:
                            pass

                    # cleanup old incomplete frames (older than 1s)
                    now = __import__('time').time()
                    to_del = [fid for fid, e in self.frames_buf.items() if now - e['time'] > 1.0]
                    for fid in to_del:
                        del self.frames_buf[fid]

            except Exception:
                # Stop listening upon requesting PAUSE or TEARDOWN
                if hasattr(self, 'playEvent') and self.playEvent.isSet():
                    break

                # Upon receiving ACK for TEARDOWN request,
                # close the RTP socket
                if self.teardownAcked == 1:
                    try:
                        self.rtpSocket.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
                    try:
                        self.rtpSocket.close()
                    except Exception:
                        pass
                    break

    def writeFrame(self, data):
        """Write the received frame to a temp image file. Return the image file."""
        cachename = CACHE_FILE_NAME + str(self.sessionId) + CACHE_FILE_EXT
        with open(cachename, "wb") as file:
            file.write(data)
        return cachename

    def playbackThread(self):
        """Consume buffered frames at a steady rate based on `RTP_FPS`.
        This prevents burst catch-up and ensures steady playback.
        """
        import time as _time, os
        try:
            fps = int(os.getenv('RTP_FPS', '25'))
        except Exception:
            fps = 25
        if fps <= 0:
            fps = 25
        frame_interval = 1.0 / float(fps)

        # initial buffer: number of frames equivalent to jitter_ms
        target_buffer_frames = max(1, int((self.jitter_ms / 1000.0) * fps))
        buffer_wait_start = _time.time()
        while True:
            if hasattr(self, 'playEvent') and self.playEvent.isSet():
                return
            with self.play_lock:
                qlen = len(self.play_queue)
            if qlen >= target_buffer_frames:
                break
            if _time.time() - buffer_wait_start > 2.0:
                break
            _time.sleep(0.01)

        while True:
            if hasattr(self, 'playEvent') and self.playEvent.isSet():
                break

            tick_start = _time.time()
            with self.play_lock:
                if self.play_queue:
                    ts, frame_bytes = heapq.heappop(self.play_queue)
                else:
                    frame_bytes = None

            if frame_bytes is not None:
                try:
                    self.updateMovie(self.writeFrame(frame_bytes))
                    self.frames_displayed += 1
                except Exception:
                    self.frames_dropped += 1

            elapsed = _time.time() - tick_start
            to_sleep = frame_interval - elapsed
            if to_sleep > 0:
                end_time = _time.time() + to_sleep
                while _time.time() < end_time:
                    if hasattr(self, 'playEvent') and self.playEvent.isSet():
                        return
                    _time.sleep(0.005)

    def updateMovie(self, imageFile):
        """Update the image file as video frame in the GUI."""
        photo = ImageTk.PhotoImage(Image.open(imageFile))
        self.label.configure(image = photo, height=288)
        self.label.image = photo

    def connectToServer(self):
        """Connect to the Server. Start a new RTSP/TCP session."""
        self.rtspSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.rtspSocket.connect((self.serverAddr, self.serverPort))
        except Exception:
            tkinter.messagebox.showwarning('Connection Failed', 'Connection to \'%s\' failed.' %self.serverAddr)

    def sendRtspRequest(self, requestCode):
        """Send RTSP request to the server."""
        request = None

        # Setup request
        if requestCode == self.SETUP and self.state == self.INIT:
            # Start thread to receive RTSP replies
            threading.Thread(target=self.recvRtspReply).start()

            # Update RTSP sequence number.
            self.rtspSeq += 1

            # Write the RTSP request to be sent.
            fps = os.getenv('RTP_FPS', '25')
            request = f"SETUP {self.fileName} RTSP/1.0\r\nCSeq: {self.rtspSeq}\r\nTransport: RTP/UDP; client_port={self.rtpPort}\r\nFPS: {fps}\r\n\r\n"

            # Keep track of the sent request.
            self.requestSent = self.SETUP

        # Play request
        elif requestCode == self.PLAY and self.state == self.READY:
            # Update RTSP sequence number.
            self.rtspSeq += 1

            # Write the RTSP request to be sent.
            request = f"PLAY {self.fileName} RTSP/1.0\r\nCSeq: {self.rtspSeq}\r\nSession: {self.sessionId}\r\n\r\n"

            # Keep track of the sent request.
            self.requestSent = self.PLAY

        # Pause request
        elif requestCode == self.PAUSE and self.state == self.PLAYING:
            # Update RTSP sequence number.
            self.rtspSeq += 1

            # Write the RTSP request to be sent.
            request = f"PAUSE {self.fileName} RTSP/1.0\r\nCSeq: {self.rtspSeq}\r\nSession: {self.sessionId}\r\n\r\n"

            # Keep track of the sent request.
            self.requestSent = self.PAUSE

        # Teardown request
        elif requestCode == self.TEARDOWN and not self.state == self.INIT:
            # Update RTSP sequence number.
            self.rtspSeq += 1

            # Write the RTSP request to be sent.
            request = f"TEARDOWN {self.fileName} RTSP/1.0\r\nCSeq: {self.rtspSeq}\r\nSession: {self.sessionId}\r\n\r\n"

            # Keep track of the sent request.
            self.requestSent = self.TEARDOWN
        else:
            return

        # Send the RTSP request using rtspSocket.
        try:
            self.rtspSocket.send(request.encode())
            print(f"RTP socket bound on UDP port {self.rtpPort}")
        except Exception:
            # If send fails, show warning
            tkinter.messagebox.showwarning('Send Failed', 'Failed to send RTSP request.')
            return

        print('\nData sent:\n' + request)

    def recvRtspReply(self):
        """Receive RTSP reply from the server."""
        while True:
            try:
                reply = self.rtspSocket.recv(1024)
            except Exception:
                break

            if reply:
                # Debug: show raw RTSP reply
                try:
                    text = reply.decode("utf-8", errors="replace")
                    print("\nRTSP Reply received:\n" + text)
                    self.parseRtspReply(text)
                except Exception:
                    traceback.print_exc()

            # Close the RTSP socket upon requesting Teardown
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
        """Parse the RTSP reply from the server."""
        lines = data.split('\n')
        # basic safety checks
        if len(lines) < 3:
            return

        try:
            seqNum = int(lines[1].split(' ')[1])
        except Exception:
            return

        # Process only if the server reply's sequence number is the same as the request's
        if seqNum == self.rtspSeq:
            try:
                session = int(lines[2].split(' ')[1])
            except Exception:
                session = 0

            # New RTSP session ID
            if self.sessionId == 0:
                self.sessionId = session

            # Process only if the session ID is the same
            if self.sessionId == session:
                try:
                    code = int(lines[0].split(' ')[1])
                except Exception:
                    return

                if code == 200:
                    if self.requestSent == self.SETUP:
                        # Update RTSP state.
                        self.state = self.READY

                        # Open RTP port.
                        self.openRtpPort()
                    elif self.requestSent == self.PLAY:
                        self.state = self.PLAYING
                    elif self.requestSent == self.PAUSE:
                        self.state = self.READY

                        # The play thread exits. A new thread is created on resume.
                        if hasattr(self, 'playEvent'):
                            self.playEvent.set()
                    elif self.requestSent == self.TEARDOWN:
                        self.state = self.INIT

                        # Flag the teardownAcked to close the socket.
                        self.teardownAcked = 1

    def openRtpPort(self):
        """Open RTP socket binded to a specified port."""
        # Create a new datagram socket to receive RTP packets from the server
        self.rtpSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # Set the timeout value of the socket to 0.5sec
        self.rtpSocket.settimeout(0.5)

        try:
            # Bind the socket to the address using the RTP port given by the client user
            self.rtpSocket.bind(('', self.rtpPort))
        except Exception:
            tkinter.messagebox.showwarning('Unable to Bind', 'Unable to bind PORT=%d' % self.rtpPort)

    def handler(self):
        """Handler on explicitly closing the GUI window."""
        self.pauseMovie()
        if tkinter.messagebox.askokcancel("Quit?", "Are you sure you want to quit?"):
            self.exitClient()
        else: # When the user presses cancel, resume playing.
            self.playMovie()
