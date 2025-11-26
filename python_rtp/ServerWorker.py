from random import randint
import sys, traceback, threading, socket, os

from VideoStream import VideoStream
from RtpPacket import RtpPacket

class ServerWorker:
	SETUP = 'SETUP'
	PLAY = 'PLAY'
	PAUSE = 'PAUSE'
	TEARDOWN = 'TEARDOWN'
	
	INIT = 0
	READY = 1
	PLAYING = 2
	state = INIT

	OK_200 = 0
	FILE_NOT_FOUND_404 = 1
	CON_ERR_500 = 2
	
	clientInfo = {}
	
	def __init__(self, clientInfo):
		self.clientInfo = clientInfo
		
	def run(self):
		threading.Thread(target=self.recvRtspRequest).start()
	
	def recvRtspRequest(self):
		"""Receive RTSP request from the client."""
		connSocket = self.clientInfo['rtspSocket'][0]
		while True:            
			data = connSocket.recv(256)
			if data:
				print("Data received:\n" + data.decode("utf-8"))
				self.processRtspRequest(data.decode("utf-8"))
	
	def processRtspRequest(self, data):
		"""Process RTSP request sent from the client."""
		# Get the request type
		request = data.split('\n') 
		line1 = request[0].split(' ')
		requestType = line1[0]
		
		# Get the media file name
		filename = line1[1]
		
		# Get the RTSP sequence number 
		seq = request[1].split(' ')
		
		# Process SETUP request
		if requestType == self.SETUP:
			if self.state == self.INIT:
				print("processing SETUP\n")

				try:
					self.clientInfo['videoStream'] = VideoStream(filename)
					self.state = self.READY
				except IOError:
					self.replyRtsp(self.FILE_NOT_FOUND_404, seq[1])

				self.clientInfo['session'] = randint(100000, 999999)
				self.replyRtsp(self.OK_200, seq[1])

				# === FIX LỖI Ở ĐÂY ===
				transportLine = request[2]
				parts = transportLine.split(';')
				for p in parts:
					if "client_port" in p:
						self.clientInfo['rtpPort'] = p.split('=')[1].strip()
						break

				# parse optional FPS header from SETUP
				for line in request:
					if line.strip().upper().startswith('FPS:'):
						try:
							self.clientInfo['fps'] = int(line.split(':', 1)[1].strip())
						except Exception:
							pass

		
		# Process PLAY request 		
		elif requestType == self.PLAY:
			if self.state == self.READY:
				print("processing PLAY\n")
				self.state = self.PLAYING

				# Prepare RTP state for this client
				self.clientInfo["rtpSocket"] = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
				# per-client RTP packet sequence (increments per RTP packet)
				self.clientInfo['rtp_seq'] = 0
				# per-client frame id counter
				self.clientInfo['frame_id'] = 0
				# stats
				self.clientInfo['packets_sent'] = 0
				self.clientInfo['bytes_sent'] = 0
				
				self.replyRtsp(self.OK_200, seq[1])
				
				# Create a new thread and start sending RTP packets
				self.clientInfo['event'] = threading.Event()
				self.clientInfo['worker']= threading.Thread(target=self.sendRtp)
				self.clientInfo['worker'].start()
		
		# Process PAUSE request
		elif requestType == self.PAUSE:
			if self.state == self.PLAYING:
				print("processing PAUSE\n")
				self.state = self.READY
				
				self.clientInfo['event'].set()
			
				self.replyRtsp(self.OK_200, seq[1])
		
		# Process TEARDOWN request
		elif requestType == self.TEARDOWN:
			print("processing TEARDOWN\n")

			self.clientInfo['event'].set()
			
			self.replyRtsp(self.OK_200, seq[1])
			
			# Close the RTP socket
			self.clientInfo['rtpSocket'].close()

		# Process REPORT request (stats from client)
		elif requestType == 'REPORT':
			print("Received REPORT from client:")
			for line in request[1:]:
				line = line.strip()
				if not line:
					continue
				print('  ' + line)
			# Reply OK
			try:
				self.replyRtsp(self.OK_200, seq[1])
			except Exception:
				pass
			
	def sendRtp(self):
		"""Send RTP packets over UDP."""
		# fragmentation parameters
		MTU = 1400
		RTP_HEADER = 12
		FRAG_HDR = 8  # 4 bytes frame_id, 2 bytes frag_idx, 2 bytes total_frags
		PAYLOAD_PER_PACKET = MTU - RTP_HEADER - FRAG_HDR

		while True:
			self.clientInfo['event'].wait(0.01)

			# Stop sending if request is PAUSE or TEARDOWN
			if self.clientInfo['event'].isSet():
				break

			data = self.clientInfo['videoStream'].nextFrame()
			if not data:
				continue

			# Prepare fragmentation
			frame_id = self.clientInfo.get('frame_id', 0) + 1
			self.clientInfo['frame_id'] = frame_id
			frame_len = len(data)
			total = (frame_len + PAYLOAD_PER_PACKET - 1) // PAYLOAD_PER_PACKET

			# determine address/port
			address = self.clientInfo['rtspSocket'][1][0]
			port = int(self.clientInfo['rtpPort'])

			# determine fps for this session (server env fallback)
			fps = self.clientInfo.get('fps') or int(os.getenv('RTP_FPS', '25'))
			try:
				fps = int(fps)
			except Exception:
				fps = 25
			if fps <= 0:
				fps = 25

			# assign a deterministic timestamp (ms) for this frame based on frame_id
			# we use (frame_id-1) so first frame is timestamp 0
			frame_ts_ms = int((frame_id - 1) * (1000.0 / float(fps)))

			# send each fragment as its own RTP packet
			for frag_idx in range(total):
				start = frag_idx * PAYLOAD_PER_PACKET
				chunk = data[start:start+PAYLOAD_PER_PACKET]
				# custom fragment header
				frag_hdr = frame_id.to_bytes(4, 'big') + frag_idx.to_bytes(2, 'big') + total.to_bytes(2, 'big')
				packet_payload = frag_hdr + chunk
				# marker bit = 1 for last fragment of a frame
				marker = 1 if (frag_idx == total - 1) else 0
				seq = self.clientInfo.get('rtp_seq', 0)
				try:
					pkt = self.makeRtp(packet_payload, seq, marker, timestamp=frame_ts_ms)
					self.clientInfo['rtpSocket'].sendto(pkt, (address, port))
					# stats
					self.clientInfo['packets_sent'] = self.clientInfo.get('packets_sent', 0) + 1
					self.clientInfo['bytes_sent'] = self.clientInfo.get('bytes_sent', 0) + len(pkt)
					# increment rtp_seq
					self.clientInfo['rtp_seq'] = (seq + 1) & 0xFFFF
				except Exception:
					print("Connection Error")

			# Pace sending to target frame rate (allow event to interrupt)
			frame_interval = 1.0 / float(fps)
			self.clientInfo['event'].wait(frame_interval)
	def makeRtp(self, payload, seqnum, marker, timestamp=None):
		"""RTP-packetize the video data with given sequence number and marker."""
		version = 2
		padding = 0
		extension = 0
		cc = 0
		pt = 26 # MJPEG type
		ssrc = 0

		rtpPacket = RtpPacket()
		rtpPacket.encode(version, padding, extension, cc, seqnum, marker, pt, ssrc, payload, timestamp=timestamp)

		return rtpPacket.getPacket()
		
	def replyRtsp(self, code, seq):
		"""Send RTSP reply to the client."""
		if code == self.OK_200:
			#print("200 OK")
			reply = 'RTSP/1.0 200 OK\nCSeq: ' + seq + '\nSession: ' + str(self.clientInfo['session'])
			connSocket = self.clientInfo['rtspSocket'][0]
			connSocket.send(reply.encode())
		
		# Error messages
		elif code == self.FILE_NOT_FOUND_404:
			print("404 NOT FOUND")
		elif code == self.CON_ERR_500:
			print("500 CONNECTION ERROR")
