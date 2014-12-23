#!/usr/bin/env python
# encoding: utf-8
"""almusic.py

Created by Aldebaran Boston Studio.
"""
import qi
import os
import string
import time
import functools
import uuid
import grooveshark
import logging as logger

class SimpleSong(object):
    """Class SimpleSong

    Contains simple information about a song.
    """
    def __init__(self, song):

        self.title = song.name.encode('utf8', 'replace')
        self.artist = song.artist.name.encode('utf8', 'replace')
        self.album = song.album.name.encode('utf8', 'replace')
        self.duration = song.duration
        self.cover = song.album._cover_url
        self.id = str(uuid.uuid1())
        self.cache_path = os.path.expanduser('~/.almusic_cache')
        self.path = self.fetch(song)

    def __str__(self):
        return '{} by {}. {}'.format(self.title, self.artist, self.album)

    def __dict__(self):
        return {'title':self.title,
                'artist':self.artist,
                'album':self.album,
                'cover':self.cover,
                'duration':self.duration,
                'id':self.id}

    def fetch(self, song):
        """Downloads a song and returns a path to a file."""
        song_file_name = "{} - {}".format(self.id, _make_file_name(song))

        song_path = os.path.join(self.cache_path,
                                 song_file_name)

        if not os.path.exists(song_path):
            with open(song_path, 'w') as song_file:
                data = song.safe_download()
                song_file.write(data)
        return song_path

@qi.multiThreaded()
class ALMusic(object):
    """Class: ALMusic

    Pump up the jam

    IMPORTANT: ALMusic module is based on qimessaging, therefore
    session.service must be used (instead of ALProxy).

    """

    def __init__(self, session):
        self.session = session
        logger.basicConfig(filename='ALAdvancedTouch.log', level=logger.DEBUG)
        self.logger = logger

        ## Connect services
        self.serv_timeout = 300 * 1000000
        self.run = True
        self.memory = None
        self.audio_player = None
        self.tts = None
        self._connect_services()
        self.memory.declareEvent('ALMusic/onQueueChange')
        

        ## Initialize cache directory where songs will be temporarily stored.

        self.cache_path = os.path.expanduser('~/.almusic_cache')
        if not os.path.isdir(self.cache_path):
            os.mkdir(self.cache_path)

        self.client = grooveshark.Client()
        self.client.init()
        self.song_queue = []
        self.active_song = None
        self.previous_songs = []
        self.player_ids = []
        self.playing = False
        self.radio_names = None
        self._init_radio_names()
        self.periodic = None
        self.say_song_name = False
        self.volume = 1.0
        self.pan = 0


    def _connect_services(self):
        """Attempt to get references to other services (avoid race conditions).
        """

        def timeout():
            """Give up connecting to services on timeout."""
            self.run = False
        qi.async(timeout, delay=self.serv_timeout)

        while self.run:
            try:
                self.memory = self.session.service('ALMemory')
                self.audio_player = self.session.service('ALAudioPlayer')
                self.tts = self.session.service('ALTextToSpeech')
                break
            except RuntimeError as err:
                time.sleep(1)
                self.logger.warning('missing:\n {}'.format(err))

        
    @qi.bind(returnType=qi.Float,
             paramsType=(qi.Float,),
             methodName="setVolume")
    def set_volume(self, volume):
        """Set the volume of the songs to be played"""
        self.volume = max([0, min([volume, 1])])
        return self.volume


    @qi.bind(returnType=qi.Float, paramsType=(qi.Float,), methodName="setPan")
    def set_pan(self, pan):
        """Set the pan of the songs to be played"""
        self.pan = max([-1, min([pan, 1])])
        return self.pan


    @qi.bind(returnType=qi.Map(qi.String, qi.String),
             paramsType=(qi.String,),
             methodName="play")
    def play(self, search_string):
        """Searches for a song and plays it."""
        self.clear_queue()
        success = self.enqueue(search_string)
        if success:
            self.play_queue()
        return success


    @qi.bind(returnType=qi.Map(qi.String, qi.String),
             paramsType=(qi.String,),
             methodName="enqueue")
    def enqueue(self, search_string):
        """Add song to the queue."""
        song_search = self.client.search(search_string)
        try:
            song = SimpleSong(song_search.next())
            self.song_queue.append(song)
            self.memory.raiseEvent('ALMusic/onQueueChange', 'add')
            return song.__dict__()
        except StopIteration:
            return {}


    @qi.bind(returnType=qi.Map(qi.String, qi.List(qi.Map(qi.String, qi.String))),
             methodName="getQueue")
    def get_queue(self):
        """Get current queue as a list of dictionaries."""

        queue = [s.__dict__() for s in self.song_queue]
        try:
            active = [self.active_song.__dict__()]
        except AttributeError:
            active = []

        return {'queue':queue,
                'active':active}


    @qi.bind(returnType=qi.Bool, methodName="play")
    def play_queue(self):
        """Plays the queue until it is empty."""
        if not self.playing:
            self.playing = True
            def go_through_queue():
                """Pop the queue while there are songs in it"""
                while self.song_queue and self.playing:
                    self.pop_queue()
                self.playing = False
                self.active_song = None
                self.memory.raiseEvent('ALMusic/onQueueChange', 'remove')
            qi.async(go_through_queue)
        return self.playing

    @qi.bind(returnType=qi.Bool, methodName="isPlaying")
    def is_playing(self):
        """Returns true is queue is playing. False otherwhise"""
        return self.playing

    @qi.bind(returnType=qi.Bool, paramsType=(qi.String,), methodName="radio")
    def play_radio(self, station):
        """Plays a radio station ad nauseam."""
        if station == 'popular':
            radio = self.client.popular()
        else:
            try:
                radio = self.client.radio(self.radio_names[station])
            except KeyError:
                self.logger.warning('Invalid station name: {}'.format(station))
                return False

        self.clear_queue()
        try:
            try:
                song = SimpleSong(radio.song)
                func = functools.partial(self._maintain_radio_queue,
                                         song_generator=radio,
                                         count=3)
            except AttributeError:
                song = SimpleSong(radio.next())
                func = functools.partial(self._maintain_popular_queue,
                                         song_generator=radio,
                                         count=3)
            self.song_queue.append(song)
            qi.async(self.play_queue)
            self.periodic = qi.PeriodicTask()
            self.periodic.setCallback(func)
            self.periodic.setUsPeriod(15000000)
            self.periodic.start(True)
            return True
        except StopIteration:
            return False


    def _maintain_radio_queue(self, song_generator, count):
        """Maintains the queue to be of length "count". """
        while len(self.song_queue) < count:
            try:
                song = SimpleSong(song_generator.song)
                self.song_queue.append(song)  
            except StopIteration:
                self.periodic.stop()

    def _maintain_popular_queue(self, song_generator, count):
        """Maintains the queue to be of length "count". """
        while len(self.song_queue) < count:
            try:
                song = SimpleSong(song_generator.next())
                self.song_queue.append(song)  
            except StopIteration:
                self.periodic.stop()


    @qi.nobind
    def pop_queue(self):
        """Plays first item in the queue."""
        self.active_song = self.song_queue.pop(0)
        self.memory.raiseEvent('ALMusic/onQueueChange', 'remove')
        path = self.active_song.path
        self.previous_songs.append(self.active_song)
        self.audio_player.playFile(path, self.volume, self.pan)
        self.memory.raiseEvent('ALMusic/onQueueChange', 'remove')
        _delete_file(path)
        return self.active_song


    @qi.bind(returnType=qi.Map(qi.String, qi.String),
             paramsType=(qi.String, qi.Int32),
             methodName="enqueueAt")
    def enqueue_at(self, search_string, position):
        """Adds song to a position in the queue."""
        song_search = self.client.search(search_string)
        try:
            song = SimpleSong(song_search.next())
            self.song_queue.insert(position, song)
            self.memory.raiseEvent('ALMusic/onQueueChange', 'add')
            return song.__dict__()
        except StopIteration:
            return {}
        except IndexError:
            self.logger.warning('Invalid index: {}'.format(position))
            return {}


    @qi.bind(methodName="clearQueue")
    def clear_queue(self):
        """Clears queue."""
        self.song_queue = []
        self.memory.raiseEvent('ALMusic/onQueueChange', 'clear')
        self._clear_cache()


    @qi.bind(methodName="next")
    def next(self):
        """Stops curently playing song or radio."""
        self.audio_player.stopAll()


    @qi.bind(methodName="stop")
    def stop(self):
        """Stops playing music."""
        if self.periodic:
            self.periodic.stop()
        self.playing = False
        self.next()
        

    @qi.nobind
    def pause(self):
        """Pauses current song or radio. Not implemented :( """
        pass

    @qi.nobind
    def previous(self):
        """Plays next song in current mix. Not implemented :( """
        pass


    @qi.nobind
    def enable_say_song_name(self):
        """ALMusic uses ALTextToSpeech to enunciate the song name and artist
        before playing it.
        """
        self.say_song_name = True


    @qi.nobind
    def disable_say_song_name(self):
        """ALMusic does not use ALTextToSpeech to enunciate the song name and
        artist before playing it."""
        self.say_song_name = False


    def _clear_cache(self):
        """Clears the song cache. Not implemented :( ."""
        for song in os.listdir(self.cache_path):
            f_path = os.path.join(self.cache_path, song)
            if os.path.isfile(f_path):
                os.unlink(f_path)


    @qi.nobind
    def _maintain_cache(self, max_size):
        """Removes old files form the cache until they cache size is less than
        or equal to the max_size.
        """
        pass


    @qi.bind(returnType=qi.List(qi.String), methodName="getRadioStations")
    def get_radio_stations(self):
        """Returns the possible radio station names."""
        return self.radio_names.keys()


    @qi.bind(returnType=qi.List(qi.Map(qi.String, qi.String)),
             paramsType=(qi.String, qi.Int32),
             methodName="search")
    def search(self, query, results):
        """Returns song search results for a given query."""
        song_search = self.client.search(query)
        search_results = list()
        while len(search_results) < results:

            try:
                song = SimpleSong(song_search.next())
                search_results.append(song.__dict__())
            except StopIteration:
                break

        return search_results


    @qi.nobind
    def _init_radio_names(self):
        """Sets the radio station names."""
        self.radio_names = {
            'k pop': grooveshark.Radio.GENRE_KPOP,
            'chinese': grooveshark.Radio.GENRE_CHINESE,
            'ragga': grooveshark.Radio.GENRE_RAGGA,
            'dance': grooveshark.Radio.GENRE_DANCE,
            'orchestra': grooveshark.Radio.GENRE_ORCHESTRA,
            'neo folk': grooveshark.Radio.GENRE_NEOFOLK,
            'post rock': grooveshark.Radio.GENRE_POSTROCK,
            'meditation': grooveshark.Radio.GENRE_MEDITATION,
            'synthpop': grooveshark.Radio.GENRE_SYNTHPOP,
            'bhangra': grooveshark.Radio.GENRE_BHANGRA,
            'samba': grooveshark.Radio.GENRE_SAMBA,
            'acapella': grooveshark.Radio.GENRE_ACAPELLA,
            'turkish': grooveshark.Radio.GENRE_TURKISH,
            'jazz blues': grooveshark.Radio.GENRE_JAZZBLUES,
            'ska': grooveshark.Radio.GENRE_SKA,
            'symphonic metal': grooveshark.Radio.GENRE_SYMPHONICMETAL,
            'dance hall': grooveshark.Radio.GENRE_DANCEHALL,
            'mpb': grooveshark.Radio.GENRE_MPB,
            'beat': grooveshark.Radio.GENRE_BEAT,
            'rnb': grooveshark.Radio.GENRE_RNB,
            'jazz': grooveshark.Radio.GENRE_JAZZ,
            'acid jazz': grooveshark.Radio.GENRE_ACIDJAZZ,
            'underground': grooveshark.Radio.GENRE_UNDERGROUND,
            'psychobilly': grooveshark.Radio.GENRE_PSYCHOBILLY,
            'desi': grooveshark.Radio.GENRE_DESI,
            'world': grooveshark.Radio.GENRE_WORLD,
            'indiefolk': grooveshark.Radio.GENRE_INDIEFOLK,
            'banda': grooveshark.Radio.GENRE_BANDA,
            'jpop': grooveshark.Radio.GENRE_JPOP,
            'progressive': grooveshark.Radio.GENRE_PROGRESSIVE,
            'black metal': grooveshark.Radio.GENRE_BLACKMETAL,
            'ska punk': grooveshark.Radio.GENRE_SKAPUNK,
            'emo': grooveshark.Radio.GENRE_EMO,
            'blues rock': grooveshark.Radio.GENRE_BLUESROCK,
            'disco': grooveshark.Radio.GENRE_DISCO,
            'opera': grooveshark.Radio.GENRE_OPERA,
            'hard style': grooveshark.Radio.GENRE_HARDSTYLE,
            '40s': grooveshark.Radio.GENRE_40S,
            'minimal': grooveshark.Radio.GENRE_MINIMAL,
            'rock': grooveshark.Radio.GENRE_ROCK,
            'acoustic': grooveshark.Radio.GENRE_ACOUSTIC,
            'gospel': grooveshark.Radio.GENRE_GOSPEL,
            'nu jazz': grooveshark.Radio.GENRE_NUJAZZ,
            'classical': grooveshark.Radio.GENRE_CLASSICAL,
            'house': grooveshark.Radio.GENRE_HOUSE,
            'dubstep': grooveshark.Radio.GENRE_DUBSTEP,
            'math rock': grooveshark.Radio.GENRE_MATHROCK,
            'blues': grooveshark.Radio.GENRE_BLUES,
            'vallenato': grooveshark.Radio.GENRE_VALLENATO,
            'folk': grooveshark.Radio.GENRE_FOLK,
            'christian rock': grooveshark.Radio.GENRE_CHRISTIANROCK,
            '90s': grooveshark.Radio.GENRE_90S,
            'heavy metal': grooveshark.Radio.GENRE_HEAVYMETAL,
            'tejano': grooveshark.Radio.GENRE_TEJANO,
            'electronica': grooveshark.Radio.GENRE_ELECTRONICA,
            'motown': grooveshark.Radio.GENRE_MOTOWN,
            'goa': grooveshark.Radio.GENRE_GOA,
            'soft rock': grooveshark.Radio.GENRE_SOFTROCK,
            'southern rock': grooveshark.Radio.GENRE_SOUTHERNROCK,
            'rb': grooveshark.Radio.GENRE_RB,
            'christmas': grooveshark.Radio.GENRE_CHRISTMAS,
            'disney': grooveshark.Radio.GENRE_DISNEY,
            'videogame': grooveshark.Radio.GENRE_VIDEOGAME,
            'noise': grooveshark.Radio.GENRE_NOISE,
            'christian': grooveshark.Radio.GENRE_CHRISTIAN,
            'bass': grooveshark.Radio.GENRE_BASS,
            'oldies': grooveshark.Radio.GENRE_OLDIES,
            'singer song writer': grooveshark.Radio.GENRE_SINGERSONGWRITER,
            'smooth jazz': grooveshark.Radio.GENRE_SMOOTHJAZZ,
            '70s': grooveshark.Radio.GENRE_70S,
            'techno': grooveshark.Radio.GENRE_TECHNO,
            'pagode': grooveshark.Radio.GENRE_PAGODE,
            'pop rock': grooveshark.Radio.GENRE_POPROCK,
            'screamo': grooveshark.Radio.GENRE_SCREAMO,
        'contemporary christian': grooveshark.Radio.GENRE_CONTEMPORARYCHRISTIAN,
            'downtempo': grooveshark.Radio.GENRE_DOWNTEMPO,
            'classic country': grooveshark.Radio.GENRE_CLASSICCOUNTRY,
            'soundtrack': grooveshark.Radio.GENRE_SOUNDTRACK,
            'oi': grooveshark.Radio.GENRE_OI,
            'christian metal': grooveshark.Radio.GENRE_CHRISTIANMETAL,
            'country': grooveshark.Radio.GENRE_COUNTRY,
            'thrash metal': grooveshark.Radio.GENRE_THRASHMETAL,
            'funky': grooveshark.Radio.GENRE_FUNKY,
            'punk rock': grooveshark.Radio.GENRE_PUNKROCK,
            'anime': grooveshark.Radio.GENRE_ANIME,
            'swing': grooveshark.Radio.GENRE_SWING,
            'classic rock': grooveshark.Radio.GENRE_CLASSICROCK,
            'post hardcore': grooveshark.Radio.GENRE_POSTHARDCORE,
            'experimental': grooveshark.Radio.GENRE_EXPERIMENTAL,
            'industrial': grooveshark.Radio.GENRE_INDUSTRIAL,
            'americana': grooveshark.Radio.GENRE_AMERICANA,
            'pop': grooveshark.Radio.GENRE_POP,
            'jesus': grooveshark.Radio.GENRE_JESUS,
            'alternativerock': grooveshark.Radio.GENRE_ALTERNATIVEROCK,
            'medieval': grooveshark.Radio.GENRE_MEDIEVAL,
            'texascountry': grooveshark.Radio.GENRE_TEXASCOUNTRY,
            'rave': grooveshark.Radio.GENRE_RAVE,
            'electronic': grooveshark.Radio.GENRE_ELECTRONIC,
            'powermetal': grooveshark.Radio.GENRE_POWERMETAL,
            'chanson': grooveshark.Radio.GENRE_CHANSON,
            'dnb': grooveshark.Radio.GENRE_DNB,
            'crunk': grooveshark.Radio.GENRE_CRUNK,
            'dub': grooveshark.Radio.GENRE_DUB,
            'grime': grooveshark.Radio.GENRE_GRIME,
            'tango': grooveshark.Radio.GENRE_TANGO,
            'schlager': grooveshark.Radio.GENRE_SCHLAGER,
            'death metal': grooveshark.Radio.GENRE_DEATHMETAL,
            'chillout': grooveshark.Radio.GENRE_CHILLOUT,
            'melodic': grooveshark.Radio.GENRE_MELODIC,
            'reggaeton': grooveshark.Radio.GENRE_REGGAETON,
            'grunge': grooveshark.Radio.GENRE_GRUNGE,
            'indie pop': grooveshark.Radio.GENRE_INDIEPOP,
            'relax': grooveshark.Radio.GENRE_RELAX,
            'club': grooveshark.Radio.GENRE_CLUB,
            'pop punk': grooveshark.Radio.GENRE_POPPUNK,
            'hard core': grooveshark.Radio.GENRE_HARDCORE,
            'indie rock': grooveshark.Radio.GENRE_INDIEROCK,
            'funk': grooveshark.Radio.GENRE_FUNK,
            'neo soul': grooveshark.Radio.GENRE_NEOSOUL,
            'trip hop': grooveshark.Radio.GENRE_TRIPHOP,
            'j rock': grooveshark.Radio.GENRE_JROCK,
            'merengue': grooveshark.Radio.GENRE_MERENGUE,
            'soul': grooveshark.Radio.GENRE_SOUL,
            'rumba': grooveshark.Radio.GENRE_RUMBA,
            'progressive rock': grooveshark.Radio.GENRE_PROGRESSIVEROCK,
            'eurodance': grooveshark.Radio.GENRE_EURODANCE,
            'folk rock': grooveshark.Radio.GENRE_FOLKROCK,
            'island': grooveshark.Radio.GENRE_ISLAND,
            'sertanejo': grooveshark.Radio.GENRE_SERTANEJO,
            'metal core': grooveshark.Radio.GENRE_METALCORE,
            '50s': grooveshark.Radio.GENRE_50S,
            'vocal': grooveshark.Radio.GENRE_VOCAL,
            'indie': grooveshark.Radio.GENRE_INDIE,
            'bluegrass': grooveshark.Radio.GENRE_BLUEGRASS,
            'jazz fusion': grooveshark.Radio.GENRE_JAZZFUSION,
            'darkwave': grooveshark.Radio.GENRE_DARKWAVE,
            '8bit': grooveshark.Radio.GENRE_8BIT,
            'rap': grooveshark.Radio.GENRE_RAP,
            'ambient': grooveshark.Radio.GENRE_AMBIENT,
            'flamenco': grooveshark.Radio.GENRE_FLAMENCO,
            'brit pop': grooveshark.Radio.GENRE_BRITPOP,
            'trance': grooveshark.Radio.GENRE_TRANCE,
            'numetal': grooveshark.Radio.GENRE_NUMETAL,
            'roots reggae': grooveshark.Radio.GENRE_ROOTSREGGAE,
            'lounge': grooveshark.Radio.GENRE_LOUNGE,
            '80s': grooveshark.Radio.GENRE_80S,
            'electro': grooveshark.Radio.GENRE_ELECTRO,
            'beach': grooveshark.Radio.GENRE_BEACH,
            'surf': grooveshark.Radio.GENRE_SURF,
            'reggae': grooveshark.Radio.GENRE_REGGAE,
            '60s': grooveshark.Radio.GENRE_60S,
            'dcima': grooveshark.Radio.GENRE_DCIMA,
            'rock steady': grooveshark.Radio.GENRE_ROCKSTEADY,
            'hip hop': grooveshark.Radio.GENRE_HIPHOP,
            'electro pop': grooveshark.Radio.GENRE_ELECTROPOP,
            'rockabilly': grooveshark.Radio.GENRE_ROCKABILLY,
            'salsa': grooveshark.Radio.GENRE_SALSA,
            'psychedelic': grooveshark.Radio.GENRE_PSYCHEDELIC,
            'celtic': grooveshark.Radio.GENRE_CELTIC,
            'metal': grooveshark.Radio.GENRE_METAL,
            'cumbia': grooveshark.Radio.GENRE_CUMBIA,
            'jungle': grooveshark.Radio.GENRE_JUNGLE,
            'zydeco': grooveshark.Radio.GENRE_ZYDECO
                            }

def _make_file_name(song):
    """Returns a valid file name for a song object."""
    valid_chars = "-_.() %s%s" % (string.ascii_letters, string.digits)
    fname = '{} - {}.mp3'.format(song.name.encode('utf8', 'replace'),
                                 song.artist.name.encode('utf8', 'replace'))
    fname = ''.join(c for c in fname if c in valid_chars)

    return fname


def _delete_file(f_path):
    """Deletes a file."""
    if os.path.isfile(f_path):
        os.unlink(f_path)
        return True
    else:
        return False

        
def register_as_service(service_class, robot_ip="127.0.1"):
    """Register service."""
    session = qi.Session()
    session.connect("tcp://%s:9559" % robot_ip)
    service_name = service_class.__name__
    instance = service_class(session)
    try:
        session.registerService(service_name, instance)
        print 'Successfully registered service: {}'.format(service_name)
    except RuntimeError:
        print '{} already registered, attempt re-register'.format(service_name)
        for info in session.services():
            try:
                if info['name'] == service_name:
                    session.unregisterService(info['serviceId'])
                    print "Unregistered {} as {}".format(service_name,
                                                         info['serviceId'])
                    break
            except (KeyError, IndexError):
                pass
        session.registerService(service_name, instance)
        print 'Successfully registered service: {}'.format(service_name)


def main():
    """Registers service"""
    register_as_service(ALMusic)
    app = qi.Application()
    app.run()

if __name__ == "__main__":
    main()
