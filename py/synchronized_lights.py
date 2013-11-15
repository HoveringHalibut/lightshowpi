#!/usr/bin/env python
#
# Author: Todd Giles (todd.giles@gmail.com)
#
# Feel free to use, just send any enhancements back my way ;)

"""Play any audio file and synchronize lights to the music

When executed, this script will play an audio file, as well as turn on and off 8 channels
of lights to the music (via the first 8 GPIO channels on the Rasberry Pi), based upon 
music it is playing. Many types of audio files are supported (see decoder.py below), but 
it has only been tested with wav and mp3 at the time of this writing.

The timing of the lights turning on and off are controlled based upon the frequency response
of the music being played.  A short segment of the music is analyzed via FFT to get the 
frequency response across 8 channels in the audio range.  Each light channel is then turned
on or off based upon whether the amplitude of the frequency response in the corresponding 
channel has crossed a dynamic threshold.

The threshold for each channel is "dynamic" in that it is adjusted upwards and downwards 
during the song playback based upon the frequency response amplitude of the song. This ensures
that soft songs, or even soft portions of songs will still turn all 8 channels on and off
during the song.

FFT caculation is quite CPU intensive and can adversely affect playback of songs (especially if
attempting to decode the song as well, as is the case for an mp3).  For this reason, the timing
values of the lights turning on and off is cached after it is calculated upon the first time a
new song is played.  The values are cached in a gzip'd text file in the same location as the
song itself.  Subsequent requests to play the same song will use the cached information and not
recompute the FFT, thus reducing CPU utilization dramatically and allowing for clear music 
playback of all audio file types.

Sample usage:

sudo python synchronized_lights.py --playlist=/home/pi/music/.playlist
sudo python synchronized_lights.py --file=/home/pi/music/jingle_bells.mp3

Third party dependencies:

alsaaudio: for audio output - http://pyalsaaudio.sourceforge.net/
decoder.py: decoding mp3, ogg, wma, and other audio files - https://pypi.python.org/pypi/decoder.py/1.5XB
numpy: for FFT calcuation - http://www.numpy.org/
raspberry-gpio-python: control GPIO output - https://code.google.com/p/raspberry-gpio-python/
"""

import argparse 
import csv
import fcntl
import gzip
import os
import random
from struct import unpack
import sys
import time
import wave

import alsaaudio as aa
import decoder
import numpy as np
import RPi.GPIO as GPIO

import log as l

parser = argparse.ArgumentParser()
filegroup = parser.add_mutually_exclusive_group()
filegroup.add_argument('--playlist', help='Playlist to choose song from (see check_sms for details on format)')
filegroup.add_argument('--file', help='music file to play (required if no playlist designated)')
parser.add_argument('-v', '--verbosity', type=int, choices=[0, 1, 2], default=1, help='change output verbosity')
args = parser.parse_args()
l.verbosity = args.verbosity

# Make sure one of --playlist or --file was specified
if args.file == None and args.playlist == None:
    print "One of --playlist or --file must be specified"
    sys.exit()

# Determine the file to play
file = args.file
if args.playlist != None:
    most_votes = [None, None, []]
    with open(args.playlist, 'rb') as f:
        fcntl.lockf(f, fcntl.LOCK_SH)
        playlist = csv.reader(f, delimiter='\t')
        songs = []
        for song in playlist:
            if len(song) < 2 or len(song) > 4:
                l.log('Invalid playlist', 0)
                sys.exit()
            elif len(song) == 2:
                song.append(set())
            else:
                song[2] = set(song[2].split(','))
                if len(song) == 3 and len(song[2]) >= len(most_votes[2]):
                    most_votes = song
            songs.append(song)
        fcntl.lockf(f, fcntl.LOCK_UN)

    if most_votes[0] != None:
        l.log("Most Votes: " + str(most_votes))
        file = most_votes[1]

        # Update playlist with latest votes
        with open(args.playlist, 'wb') as f:
            fcntl.lockf(f, fcntl.LOCK_EX)
            writer = csv.writer(f, delimiter='\t')
            for song in songs:
                if file == song[1] and len(song) == 3:
                    song.append("playing!")
                if len(song[2]) > 0:
                    song[2] = ",".join(song[2])
                else:
                    del song[2]
            writer.writerows(songs)
            fcntl.lockf(f, fcntl.LOCK_UN)

    else:
        file = songs[random.randint(0, len(songs)-1)][1]

# Initialize GPIO
GPIO.setwarnings(False)
GPIO.cleanup()
GPIO.setmode(GPIO.BOARD)

gpio = [11,12,13,15,16,18,22,7]
for i in gpio:
    GPIO.setup(i, GPIO.OUT)

def TurnOffLights():
    for i in range(8):
        TurnOffLight(i)

def TurnOffLight(i):
    GPIO.output(gpio[i], GPIO.LOW)

def TurnOnLight(i):
    GPIO.output(gpio[i], GPIO.HIGH)

# Initialize Lights
TurnOffLights()

# Initialize FFT stats
matrix    = [0,0,0,0,0,0,0,0]
power     = []
weighting = [2,2,4,4,8,16,32,16] # Power of 2
limit     = [5,5,5,5,5,5,5,5]
offct     = [0,0,0,0,0,0,0,0]

# Set up audio
if file.endswith('.wav'):
   musicfile = wave.open(file, 'r')
else:
   musicfile = decoder.open(file)

sample_rate  = musicfile.getframerate()
no_channels  = musicfile.getnchannels()
chunk        = 4096 # Use a multiple of 8
output       = aa.PCM(aa.PCM_PLAYBACK, aa.PCM_NORMAL)
output.setchannels(no_channels)
output.setrate(sample_rate)
output.setformat(aa.PCM_FORMAT_S16_LE)
output.setperiodsize(chunk)

# Output a bit about what we're about to play
file = os.path.abspath(file)
l.log("Playing: " + file + " (" + str(musicfile.getnframes()/sample_rate) + " sec)", 0)

# Read in cached light control signals
cache = []
cache_found = False
cache_filename = os.path.dirname(file) + "/." + os.path.basename(file) + ".sync.gz"
try:
   with gzip.open(cache_filename, 'rb') as f:
      cachefile = csv.reader(f, delimiter=',')
      for row in cachefile:
         cache.append(row)
      cache_found = True
except IOError:
   l.log("Cached sync data file not found: '" + cache_filename + ".", 1)

# Return power array index corresponding to a particular frequency
def piff(val):
   return int(2*chunk*val/sample_rate)
   
def calculate_levels(data, chunk, sample_rate):
   global matrix
   # Convert raw data (ASCII string) to numpy array
   data = unpack("%dh"%(len(data)/2),data)
   data = np.array(data, dtype='h')
   # Apply FFT - real data
   fourier=np.fft.rfft(data)
   # Remove last element in array to make it the same size as chunk
   fourier=np.delete(fourier,len(fourier)-1)
   # Find average 'amplitude' for specific frequency ranges in Hz
   power = np.abs(fourier)   
   matrix[0]= np.mean(power[piff(0)    :piff(156):1])
   matrix[1]= np.mean(power[piff(156)  :piff(313):1])
   matrix[2]= np.mean(power[piff(313)  :piff(625):1])
   matrix[3]= np.mean(power[piff(625)  :piff(1250):1])
   matrix[4]= np.mean(power[piff(1250) :piff(2500):1])
   matrix[5]= np.mean(power[piff(2500) :piff(5000):1])
   matrix[6]= np.mean(power[piff(5000) :piff(10000):1])
   matrix[7]= np.mean(power[piff(10000):piff(15000):1])
   # Tidy up column values for output to lights
   matrix=np.divide(np.multiply(matrix,weighting),100000)
   # Set floor at 0 and ceiling at 100 for lights
   matrix=matrix.clip(0,100) 
   return matrix

# Process audio file
row = 0
data = musicfile.readframes(chunk)
while data!='':
   output.write(data)

   # Control lights with cached timing values if they exist
   if cache_found:
      if row < len(cache):
         entry = cache[row]
         for i in range (0,8):
            if int(entry[i]):
               TurnOnLight(i)
            else:
               TurnOffLight(i)
      else:
         l.log("!!!! Ran out of cached timing values !!!!", 2)
         
   # No cache - Compute FFT from this chunk, and cache results
   else:
      entry = []
      matrix=calculate_levels(data, chunk, sample_rate)
      for i in range (0,8):
         if limit[i] < matrix[i] * 0.6:
            limit[i] = limit[i] * 1.2
            l.log("++++ channel: {0}; limit: {1:.3f}".format(i, limit[i]), 2)
         if matrix[i] > limit[i]:
            TurnOnLight(i)
            offct[i] = 0
            entry.append('1')
         else:
            offct[i] = offct[i]+1
            if offct[i] > 10:
               offct[i] = 0
               limit[i] = limit[i] * 0.8
            l.log("---- channel: {0}; limit: {1:.3f}".format(i, limit[i]), 2)
            TurnOffLight(i)
            entry.append('0')
      cache.append(entry)

   # Read next chunk of data from music file
   data = musicfile.readframes(chunk)
   row = row + 1

if not cache_found:
   with gzip.open(cache_filename, 'wb') as f:
      writer = csv.writer(f, delimiter=',')
      writer.writerows(cache)
      l.log("Cached sync data written to '." + cache_filename + "' [" + str(len(cache)) + " rows]", 1)

# We're done, turn it all off ;)
TurnOffLights()