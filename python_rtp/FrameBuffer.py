"""
Frame Buffer Module - Handles frame reassembly and buffering
Support for fragmented frames with timeout cleanup
"""

import queue
import threading
import os
from time import time

class FrameBuffer:
    """
    Manages RTP frame reassembly and buffering.
    - Reassembles fragmented frames (with magic header)
    - Stores complete frames in queue
    - Auto-cleanup incomplete frames (timeout)
    """
    
    # Fragment magic header (must match server)
    FRAG_MAGIC = b'FRAG'
    
    def __init__(self, maxsize=30, timeout=5.0, pre_buffer=10):
        """
        Args:
            maxsize: Max frames in buffer queue
            timeout: Incomplete frame timeout (seconds)
            pre_buffer: Frames needed before pre-buffer complete
        """
        self.frameBuffer = queue.Queue(maxsize=maxsize)
        self.maxsize = maxsize
        self.timeout = timeout
        self.pre_buffer_threshold = pre_buffer
        
        # Fragment reassembly storage: frame_id -> {'total', 'chunks', 'received', 'time', 'last_received'}
        self.frames_buf = {}
        self.buf_lock = threading.Lock()
        
        # Buffering state
        self.isBuffering = True
        self.bufferingEvent = threading.Event()
        
        # Statistics
        self.stats_lock = threading.Lock()
        self.total_frames_received = 0
        self.total_frames_buffered = 0
        self.total_packets_received = 0
        self.total_bytes_received = 0
        self.total_fragments_dropped = 0

    def add_rtp_packet(self, payload, rtp_seq):
        """
        Process incoming RTP payload (may be fragment or complete frame)
        
        Args:
            payload: RTP payload bytes
            rtp_seq: RTP sequence number (for logging)
        
        Returns:
            Tuple (is_fragment: bool, frame_id: int or None, status: str)
        """
        with self.stats_lock:
            self.total_packets_received += 1
            self.total_bytes_received += len(payload)
        
        # Check if fragmented frame (has magic header)
        if payload.startswith(self.FRAG_MAGIC):
            return self._handle_fragment(payload, rtp_seq)
        else:
            # Complete frame
            self._add_frame_to_buffer(payload)
            return (False, None, "complete_frame")

    def _handle_fragment(self, payload, rtp_seq):
        """Parse and reassemble fragment"""
        try:
            # Strip magic header then parse
            header = payload[len(self.FRAG_MAGIC):len(self.FRAG_MAGIC)+8]
            frame_id = int.from_bytes(header[0:4], 'big')
            frag_idx = int.from_bytes(header[4:6], 'big')
            total = int.from_bytes(header[6:8], 'big')
            chunk = payload[len(self.FRAG_MAGIC)+8:]
            
            with self.buf_lock:
                # Get or create reassembly entry
                entry = self.frames_buf.get(frame_id)
                if not entry:
                    entry = {
                        'total': total,
                        'chunks': {},
                        'received': set(),
                        'time': time(),
                        'last_received': time()
                    }
                    self.frames_buf[frame_id] = entry
                
                # Update last received time
                entry['last_received'] = time()
                
                # Store chunk if not received
                if frag_idx not in entry['received']:
                    entry['chunks'][frag_idx] = chunk
                    entry['received'].add(frag_idx)
                
                # Check if complete
                if len(entry['received']) == entry['total']:
                    # Reassemble frame
                    parts = [entry['chunks'][i] for i in range(entry['total'])]
                    frame_bytes = b''.join(parts)
                    
                    # Add to buffer
                    self._add_frame_to_buffer(frame_bytes)
                    
                    # Cleanup
                    del self.frames_buf[frame_id]
                    
                    return (True, frame_id, f"frame_complete_{entry['total']}_frags")
                else:
                    # Still waiting for more fragments
                    progress = len(entry['received'])
                    return (True, frame_id, f"frag_{frag_idx}/{total}")
        
        except Exception as e:
            print(f"[FrameBuffer] Error parsing fragment: {e}")
            with self.stats_lock:
                self.total_fragments_dropped += 1
            return (True, None, f"error_{str(e)}")

    def _add_frame_to_buffer(self, frame_bytes):
        """Add complete frame to buffer queue"""
        try:
            self.frameBuffer.put(frame_bytes, block=False)
            
            with self.stats_lock:
                self.total_frames_buffered += 1
                self.total_frames_received += 1
            
            buf_size = self.frameBuffer.qsize()
            
            # Check if pre-buffering complete
            if self.isBuffering and buf_size >= self.pre_buffer_threshold:
                self.isBuffering = False
                self.bufferingEvent.set()
                print(f"[FrameBuffer] Pre-buffer COMPLETE ({buf_size}/{self.maxsize} frames)")
            else:
                if buf_size % 5 == 0 or buf_size <= 2:
                    print(f"[FrameBuffer] Buffer: {buf_size}/{self.maxsize}")
        
        except queue.Full:
            # Buffer overflow - drop oldest frame
            print(f"[FrameBuffer] OVERFLOW! Dropping oldest frame...")
            try:
                old_frame = self.frameBuffer.get_nowait()
                self.frameBuffer.put(frame_bytes, block=False)
            except queue.Empty:
                pass

    def get_frame(self, timeout=0.1):
        """
        Get next frame from buffer
        
        Args:
            timeout: Blocking timeout (seconds)
        
        Returns:
            frame_bytes or None
        """
        try:
            return self.frameBuffer.get(timeout=timeout)
        except queue.Empty:
            return None

    def cleanup_incomplete_frames(self):
        """Remove incomplete frames that timed out"""
        with self.buf_lock:
            now = time()
            to_delete = []
            
            for frame_id, entry in list(self.frames_buf.items()):
                last_recv = entry.get('last_received', entry.get('time', now))
                if now - last_recv > self.timeout:
                    to_delete.append(frame_id)
            
            for frame_id in to_delete:
                entry = self.frames_buf[frame_id]
                received = len(entry['received'])
                total = entry['total']
                print(f"[FrameBuffer] Cleanup: frame {frame_id} timeout ({received}/{total} frags)")
                del self.frames_buf[frame_id]

    def get_stats(self):
        """Get current buffer statistics"""
        with self.stats_lock:
            return {
                'buffer_size': self.frameBuffer.qsize(),
                'buffer_max': self.maxsize,
                'total_frames_received': self.total_frames_received,
                'total_frames_buffered': self.total_frames_buffered,
                'total_packets_received': self.total_packets_received,
                'total_bytes_received': self.total_bytes_received / (1024*1024),  # MB
                'total_fragments_dropped': self.total_fragments_dropped,
                'incomplete_frames': len(self.frames_buf),
                'is_buffering': self.isBuffering
            }

    def reset(self):
        """Reset buffer (for new playback session)"""
        with self.buf_lock:
            self.frameBuffer = queue.Queue(maxsize=self.maxsize)
            self.frames_buf = {}
            self.isBuffering = True
            self.bufferingEvent.clear()

    def wait_pre_buffer(self, timeout=10.0):
        """Wait until pre-buffer complete or timeout"""
        return self.bufferingEvent.wait(timeout=timeout)