#!/usr/bin/env python
# vim:fileencoding=utf-8
# License: GPL v3 Copyright: 2016, Kovid Goyal <kovid at kovidgoyal.net>

import os
import weakref
from collections import deque
from functools import partial
from time import monotonic

from .char_grid import CharGrid
from .constants import wakeup, get_boss, appname, WindowGeometry, is_key_pressed, mouse_button_pressed, cell_size
from .fast_data_types import (
    BRACKETED_PASTE_START, BRACKETED_PASTE_END, Screen, read_bytes_dump,
    read_bytes, GLFW_MOD_SHIFT, GLFW_MOUSE_BUTTON_1, GLFW_PRESS,
    GLFW_MOUSE_BUTTON_MIDDLE, GLFW_RELEASE, glfw_post_empty_event,
    GLFW_MOUSE_BUTTON_5, ANY_MODE, MOTION_MODE, GLFW_KEY_LEFT_SHIFT,
    GLFW_KEY_RIGHT_SHIFT, GLFW_KEY_UP, GLFW_KEY_DOWN, GLFW_MOUSE_BUTTON_4
)
from .keys import get_key_map
from .mouse import encode_mouse_event, PRESS, RELEASE, MOVE, DRAG
from .terminfo import get_capabilities
from .utils import sanitize_title, get_primary_selection, parse_color_set, safe_print


class Window:

    def __init__(self, tab, child, opts, args):
        self.tabref = weakref.ref(tab)
        self.override_title = None
        self.last_mouse_cursor_pos = 0, 0
        self.destroyed = False
        self.click_queue = deque(maxlen=3)
        self.geometry = WindowGeometry(0, 0, 0, 0, 0, 0)
        self.needs_layout = True
        self.title = appname
        self._is_visible_in_layout = True
        self.child, self.opts = child, opts
        self.child_fd = child.child_fd
        self.start_visual_bell_at = None
        self.screen = Screen(self, 24, 80, opts.scrollback_lines)
        self.read_bytes = partial(read_bytes_dump, self.dump_commands) if args.dump_commands or args.dump_bytes else read_bytes
        if args.dump_bytes:
            self.dump_bytes_to = open(args.dump_bytes, 'ab')
        self.draw_dump_buf = []
        self.write_buf = memoryview(b'')
        self.char_grid = CharGrid(self.screen, opts)

    def __repr__(self):
        return 'Window(title={}, id={})'.format(self.title, hex(id(self)))

    @property
    def is_visible_in_layout(self):
        return self._is_visible_in_layout

    @is_visible_in_layout.setter
    def is_visible_in_layout(self, val):
        val = bool(val)
        if val != self._is_visible_in_layout:
            self._is_visible_in_layout = val
            if val:
                self.refresh()

    def refresh(self):
        self.screen.mark_as_dirty()
        wakeup()

    def set_geometry(self, new_geometry):
        if self.needs_layout or new_geometry.xnum != self.screen.columns or new_geometry.ynum != self.screen.lines:
            self.screen.resize(new_geometry.ynum, new_geometry.xnum)
            self.child.resize_pty(self.screen.columns, self.screen.lines,
                                  max(0, new_geometry.right - new_geometry.left), max(0, new_geometry.bottom - new_geometry.top))
            self.char_grid.resize(new_geometry)
            self.needs_layout = False
        else:
            self.char_grid.update_position(new_geometry)
        self.geometry = new_geometry

    def contains(self, x, y):
        g = self.geometry
        return g.left <= x <= g.right and g.top <= y <= g.bottom

    def close(self):
        get_boss().close_window(self)

    def destroy(self):
        self.destroyed = True
        self.child.hangup()
        self.child.get_child_status()  # Ensure child does not become zombie
        # At this point this window can still render to screen using its
        # existing buffers in char_grid. The rest of the cleanup must be
        # performed in the GUI thread.

    def read_ready(self):
        if self.read_bytes(self.screen, self.child_fd) is False:
            self.close()  # EOF

    def write_ready(self):
        while self.write_buf:
            try:
                n = os.write(self.child_fd, self.write_buf)
            except BlockingIOError:
                n = 0
            if not n:
                return
            self.write_buf = self.write_buf[n:]

    def write_to_child(self, data):
        self.write_buf = memoryview(self.write_buf.tobytes() + data)
        wakeup()

    def bell(self):
        if self.opts.enable_audio_bell:
            try:
                with open('/dev/tty', 'wb') as f:
                    f.write(b'\007')
            except EnvironmentError:
                pass  # failure to beep is not critical
        if self.opts.visual_bell_duration > 0:
            self.start_visual_bell_at = monotonic()
            glfw_post_empty_event()

    def use_utf8(self, on):
        self.child.set_iutf8(on)

    def update_screen(self):
        self.char_grid.update_cell_data()
        glfw_post_empty_event()

    def focus_changed(self, focused):
        if focused:
            if self.screen.focus_tracking_enabled:
                self.write_to_child(b'\x1b[I')
        else:
            if self.screen.focus_tracking_enabled:
                self.write_to_child(b'\x1b[O')

    def title_changed(self, new_title):
        if self.override_title is None:
            self.title = sanitize_title(new_title or appname)
            t = self.tabref()
            if t is not None:
                t.title_changed(self)
            glfw_post_empty_event()

    def icon_changed(self, new_icon):
        pass  # TODO: Implement this

    def set_dynamic_color(self, code, value):
        wmap = {10: 'fg', 11: 'bg', 110: 'fg', 111: 'bg'}
        if isinstance(value, bytes):
            value = value.decode('utf-8')
        color_changes = {}
        for val in value.split(';'):
            w = wmap.get(code)
            if w is not None:
                if code >= 110:
                    val = None
                color_changes[w] = val
            code += 1
        self.char_grid.change_colors(color_changes)
        glfw_post_empty_event()

    def set_color_table_color(self, code, value):
        if code == 4:
            for c, val in parse_color_set(value):
                self.char_grid.color_profile.set_color(c, val)
            self.refresh()
        elif code == 104:
            if not value.strip():
                self.char_grid.color_profile.reset_color_table()
            else:
                for c in value.split(';'):
                    try:
                        c = int(c)
                    except Exception:
                        continue
                    if 0 <= c <= 255:
                        self.char_grid.color_profile.reset_color(c)
            self.refresh()

    def request_capabilities(self, q):
        self.write_to_child(get_capabilities(q))

    def dispatch_multi_click(self, x, y):
        if len(self.click_queue) > 2 and self.click_queue[-1] - self.click_queue[-3] <= 2 * self.opts.click_interval:
            self.char_grid.multi_click(3, x, y)
            glfw_post_empty_event()
        elif len(self.click_queue) > 1 and self.click_queue[-1] - self.click_queue[-2] <= self.opts.click_interval:
            self.char_grid.multi_click(2, x, y)
            glfw_post_empty_event()

    def on_mouse_button(self, button, action, mods):
        mode = self.screen.mouse_tracking_mode()
        handle_event = mods == GLFW_MOD_SHIFT or mode == 0 or button == GLFW_MOUSE_BUTTON_MIDDLE or (
            mods == self.opts.open_url_modifiers and button == GLFW_MOUSE_BUTTON_1)
        x, y = self.last_mouse_cursor_pos
        if handle_event:
            if button == GLFW_MOUSE_BUTTON_1:
                self.char_grid.update_drag(action == GLFW_PRESS, x, y)
                if action == GLFW_RELEASE:
                    if mods == self.char_grid.opts.open_url_modifiers:
                        self.char_grid.click_url(x, y)
                    self.click_queue.append(monotonic())
                    self.dispatch_multi_click(x, y)
            elif button == GLFW_MOUSE_BUTTON_MIDDLE:
                if action == GLFW_RELEASE:
                    self.paste_from_selection()
        else:
            x, y = self.char_grid.cell_for_pos(x, y)
            if x is not None:
                ev = encode_mouse_event(mode, self.screen.mouse_tracking_protocol(),
                                        button, PRESS if action == GLFW_PRESS else RELEASE, mods, x, y)
                if ev:
                    self.write_to_child(ev)

    def on_mouse_move(self, x, y):
        button = None
        for b in range(0, GLFW_MOUSE_BUTTON_5 + 1):
            if mouse_button_pressed[b]:
                button = b
                break
        action = MOVE if button is None else DRAG
        mode = self.screen.mouse_tracking_mode()
        send_event = (mode == ANY_MODE or (mode == MOTION_MODE and button is not None)) and not (
            is_key_pressed[GLFW_KEY_LEFT_SHIFT] or is_key_pressed[GLFW_KEY_RIGHT_SHIFT])
        x, y = max(0, x - self.geometry.left), max(0, y - self.geometry.top)
        self.last_mouse_cursor_pos = x, y
        tm = get_boss()
        tm.queue_ui_action(get_boss().change_mouse_cursor, self.char_grid.has_url_at(x, y))
        if send_event:
            x, y = self.char_grid.cell_for_pos(x, y)
            if x is not None:
                ev = encode_mouse_event(mode, self.screen.mouse_tracking_protocol(),
                                        button, action, 0, x, y)
                if ev:
                    self.write_to_child(ev)
        else:
            if self.char_grid.current_selection.in_progress:
                self.char_grid.update_drag(None, x, y)
                margin = cell_size.height // 2
                if y <= margin or y >= self.geometry.bottom - margin:
                    get_boss().timers.add(0.02, self.drag_scroll)

    def drag_scroll(self):
        x, y = self.last_mouse_cursor_pos
        tm = get_boss()
        margin = cell_size.height // 2
        if y <= margin or y >= self.geometry.bottom - margin:
            self.scroll_line_up() if y < 50 else self.scroll_line_down()
            self.char_grid.update_drag(None, x, y)
            tm.timers.add(0.02, self.drag_scroll)

    def on_mouse_scroll(self, x, y):
        s = int(round(y * self.opts.wheel_scroll_multiplier))
        if abs(s) < 0:
            return
        upwards = s > 0
        if self.screen.is_main_linebuf():
            self.char_grid.scroll(abs(s), upwards)
            glfw_post_empty_event()
        else:
            mode = self.screen.mouse_tracking_mode()
            send_event = mode > 0
            if send_event:
                x, y = self.last_mouse_cursor_pos
                x, y = self.char_grid.cell_for_pos(x, y)
                if x is not None:
                    ev = encode_mouse_event(mode, self.screen.mouse_tracking_protocol(),
                                            GLFW_MOUSE_BUTTON_4 if upwards else GLFW_MOUSE_BUTTON_5, PRESS, 0, x, y)
                    if ev:
                        self.write_to_child(ev)
            else:
                k = get_key_map(self.screen)[GLFW_KEY_UP if upwards else GLFW_KEY_DOWN]
                self.write_to_child(k * abs(s))

    def buf_toggled(self, is_main_linebuf):
        self.char_grid.scroll('full', False)

    def render_cells(self, render_data, program, sprites):
        invert_colors = False
        if self.start_visual_bell_at is not None:
            invert_colors = monotonic() - self.start_visual_bell_at <= self.opts.visual_bell_duration
            if not invert_colors:
                self.start_visual_bell_at = None
        self.char_grid.render_cells(render_data, program, sprites, invert_colors)

    # actions {{{

    def show_scrollback(self):
        data = self.char_grid.get_scrollback_as_ansi()
        get_boss().display_scrollback(data)

    def paste(self, text):
        if text and not self.destroyed:
            if isinstance(text, str):
                text = text.encode('utf-8')
            if self.screen.in_bracketed_paste_mode:
                text = BRACKETED_PASTE_START.encode('ascii') + text + BRACKETED_PASTE_END.encode('ascii')
            self.write_to_child(text)

    def paste_from_selection(self):
        text = get_primary_selection()
        if text:
            if isinstance(text, bytes):
                text = text.decode('utf-8')
            self.paste(text)

    def copy_to_clipboard(self):
        text = self.char_grid.text_for_selection()
        if text:
            tm = get_boss()
            tm.queue_ui_action(tm.glfw_window.set_clipboard_string, text)

    def scroll_line_up(self):
        if self.screen.is_main_linebuf():
            self.char_grid.scroll('line', True)
            glfw_post_empty_event()

    def scroll_line_down(self):
        if self.screen.is_main_linebuf():
            self.char_grid.scroll('line', False)
            glfw_post_empty_event()

    def scroll_page_up(self):
        if self.screen.is_main_linebuf():
            self.char_grid.scroll('page', True)
            glfw_post_empty_event()

    def scroll_page_down(self):
        if self.screen.is_main_linebuf():
            self.char_grid.scroll('page', False)
            glfw_post_empty_event()

    def scroll_home(self):
        if self.screen.is_main_linebuf():
            self.char_grid.scroll('full', True)
            glfw_post_empty_event()

    def scroll_end(self):
        if self.screen.is_main_linebuf():
            self.char_grid.scroll('full', False)
            glfw_post_empty_event()
    # }}}

    def dump_commands(self, *a):  # {{{
        if a:
            if a[0] == 'draw':
                if a[1] is None:
                    if self.draw_dump_buf:
                        safe_print('draw', ''.join(self.draw_dump_buf))
                        self.draw_dump_buf = []
                else:
                    self.draw_dump_buf.append(a[1])
            elif a[0] == 'bytes':
                self.dump_bytes_to.write(a[1])
                self.dump_bytes_to.flush()
            else:
                if self.draw_dump_buf:
                    safe_print('draw', ''.join(self.draw_dump_buf))
                    self.draw_dump_buf = []
                safe_print(*a)
    # }}}
