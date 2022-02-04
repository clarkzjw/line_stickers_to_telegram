import datetime
import math
import os.path
import queue
from enum import Enum
from threading import Thread

import ffmpeg
from PIL import Image


class ProcessOption(Enum):
    NONE = 'none'
    SCALE = 'scale'
    TO_GIF = 'to_gif'
    TO_VIDEO = 'to_video'
    TO_WEBM = 'to_webm'


class ImageProcessorThread(Thread):
    def __init__(self, queue, option: ProcessOption, temp_dir):
        Thread.__init__(self, name='ImageProcessorThread')
        self.queue = queue
        self.option = option
        self.temp_dir = temp_dir

    def run(self):
        if self.option == ProcessOption.NONE:
            # this should not happen, bro. Why are you creating this thread?
            return
        while not self.queue.empty():
            try:
                in_files, out_file = self.queue.get_nowait()
            except queue.Empty:
                continue
            try:
                if self.option == ProcessOption.TO_VIDEO:
                    in_pic, in_audio = in_files
                    self.to_video(in_pic, in_audio, out_file)
                else:
                    in_file = in_files
                    if self.option == ProcessOption.SCALE:
                        self.scale_image(in_file, out_file)
                    elif self.option == ProcessOption.TO_GIF:
                        self.to_gif(in_file, out_file)
                    elif self.option == ProcessOption.TO_WEBM:
                        self.to_webm(in_file, out_file)
                    else:
                        # shouldn't get there
                        print('Unexpected Option:', self.option)
                        continue
            except ffmpeg.Error as e:
                print('Error occurred:', e, out_file)
                print('------stdout------')
                print(e.stdout.decode())
                print('------end------')
                print('------stderr------')
                print(e.stderr.decode())
                print('------end------')
            finally:
                self.queue.task_done()

    def scale_image(self, in_file, out_file):
        ffmpeg.input(in_file).filter('scale', w='if(gt(iw,ih),512,-1)', h='if(gt(iw,ih),-1,512)').output(out_file).run(
            quiet=True)

    def remove_alpha_geq(self, in_stream):
        """
        use geq filter to remove alpha
        just calculate the new color for each pixel on a white background
        slower than overlay method
        cannot handle pal8
        :param in_stream:
        :return:
        """
        return in_stream \
            .filter('geq',
                    r='(r(X,Y)*alpha(X,Y)/255)+(255-alpha(X,Y))',
                    g='(g(X,Y)*alpha(X,Y)/255)+(255-alpha(X,Y))',
                    b='(b(X,Y)*alpha(X,Y)/255)+(255-alpha(X,Y))',
                    a=255)

    def remove_alpha_overlay(self, in_stream):
        """
        overlay image on a white background
        faster than geq filter
        cannot handle ya8
        :param in_stream:
        :return:
        """
        return in_stream \
            .filter('pad', w='iw*2', h='ih', x='iw', y='ih', color='white') \
            .crop(width='iw/2', height='ih', x=0, y=0) \
            .overlay(in_stream)

    def remove_alpha(self, in_stream, pix_fmt):
        if pix_fmt == 'ya8':  # use geq method
            return self.remove_alpha_geq(in_stream)
        else:
            return self.remove_alpha_overlay(in_stream)

    def to_webm(self, in_file, out_file):

        if ffmpeg.probe(in_file)['streams'][0]['pix_fmt'] == 'pal8':
            # need to split frames, convert color, then put back
            # TODO reuse code

            new_in_file = in_file + '.convert.png'
            in_file_name = in_file.split(os.path.sep)[-1].split('.')[0]

            frame_tmp_path = os.path.join(self.temp_dir, in_file_name)
            try:
                os.mkdir(frame_tmp_path)
            except FileExistsError:
                pass

            framerate_str = ffmpeg.probe(in_file)['streams'][0]['r_frame_rate']
            framerate = round(int(framerate_str.split('/')[0]) / int(framerate_str.split('/')[1]))

            ffmpeg.input(in_file, f='apng'). \
                output(os.path.join(frame_tmp_path, 'frame-convert-%d.png'), start_number=0) \
                .overwrite_output(). \
                run(quiet=True)

            for fn in os.listdir(frame_tmp_path):
                if fn.startswith('frame-convert'):
                    fn_full = os.path.join(frame_tmp_path, fn)
                    image = Image.open(fn_full)
                    image = image.convert('RGBA')
                    image.save(fn_full)
            ffmpeg.input(os.path.join(frame_tmp_path, 'frame-convert-%d.png'), start_number=0, framerate=framerate,
                         ).output(new_in_file, f='apng').overwrite_output().run(quiet=True)
            in_file = new_in_file
        video_input = ffmpeg.input(in_file, f='apng').filter('scale', w='if(gt(iw,ih),512,-1)',
                                                             h='if(gt(iw,ih),-1,512)')

        ffmpeg.output(video_input, out_file).overwrite_output().run(quiet=True)

        # probe, ensure it's max 3 seconds
        duration_str = ffmpeg.probe(out_file)['streams'][0]['tags']['DURATION']
        framerate_str = ffmpeg.probe(out_file)['streams'][0]['r_frame_rate']
        framerate = round(int(framerate_str.split('/')[0]) / int(framerate_str.split('/')[1]))

        hms, us = duration_str.split('.')
        us = us[:6]
        duration_str = f'{hms}.{us}'
        duration_dt = datetime.datetime.strptime(duration_str, '%H:%M:%S.%f')
        duration_seconds = datetime.timedelta(seconds=duration_dt.second,
                                              microseconds=duration_dt.microsecond).total_seconds()
        total_frames = round(duration_seconds * framerate)

        if duration_seconds > 3:
            # need to speedup: split image to frames and re-generate

            new_framerate = math.ceil((total_frames + 1) / 3)
            in_file_name = in_file.split(os.path.sep)[-1].split('.')[0]

            frame_tmp_path = os.path.join(self.temp_dir, in_file_name)
            try:
                os.mkdir(frame_tmp_path)
            except FileExistsError:
                pass

            video_input.output(os.path.join(frame_tmp_path, 'frame-%d.png'), start_number=0).overwrite_output().run(
                quiet=True)
            ffmpeg.input(os.path.join(frame_tmp_path, 'frame-%d.png'), start_number=0, framerate=new_framerate,
                         ).output(out_file).overwrite_output().run(quiet=True)

    def to_gif(self, in_file, out_file):
        pix_fmt = ffmpeg.probe(in_file)['streams'][0]['pix_fmt']
        input_stream = ffmpeg.input(in_file, f='apng')
        in1 = self.remove_alpha(input_stream, pix_fmt)

        in2 = input_stream.filter('palettegen')
        ffmpeg.filter([in1, in2], 'paletteuse') \
            .output(out_file) \
            .overwrite_output() \
            .run(quiet=True)

    def to_video(self, in_pic, in_audio, out_file):
        streams = list()
        pix_fmt = ffmpeg.probe(in_pic)['streams'][0]['pix_fmt']
        in_pic_stream = ffmpeg.input(in_pic, f='apng')
        video_output = self.remove_alpha(in_pic_stream, pix_fmt) \
            .filter('scale',
                    w='trunc(iw/2)*2',
                    h='trunc(ih/2)*2'  # enable H.264
                    )

        streams.append(video_output)
        if in_audio:
            audio_input = ffmpeg.input(in_audio)
            streams.append(audio_input)
        ffmpeg.output(*streams, out_file, pix_fmt='yuv420p', movflags='faststart') \
            .overwrite_output().run(quiet=True)


def process_video_sticker_icon(in_file, out_file):
    ffmpeg.input(in_file, f='apng') \
        .filter('scale', w='if(gt(iw,ih),100,-1)', h='if(gt(iw,ih),-1,100)') \
        .filter('pad', w='100', h='100', x='(ow-iw)/2', y='(oh-ih)/2', color='black@0') \
        .output(out_file).overwrite_output().run(quiet=True)
