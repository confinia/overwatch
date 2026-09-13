"""Decoders not (yet) shipped by the satnogs-decoders release we pin (#458).

`_decode_frame` looks in `satnogsdecoders.decoder` first and falls back to
this package, so a decoder lands here the day its upstream merge request is
opened and leaves the day a release carries it: bump the pin, delete the
file. Each module is the kaitai-struct-compiler output of a `.ksy` and says
where that `.ksy` lives, who wrote it and under which licence.
"""
