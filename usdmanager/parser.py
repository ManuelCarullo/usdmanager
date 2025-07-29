#
# Copyright 2020 DreamWorks Animation L.L.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""
File parsers
"""

import logging
import re
import traceback
from collections import defaultdict
from xml.sax.saxutils import escape, unescape

from Qt.QtCore import QFile, QFileInfo, QIODevice, QObject, QTextStream, Signal, Slot
from Qt.QtGui import QIcon

from .constants import LINE_CHAR_LIMIT, CHAR_LIMIT, FILE_FORMAT_NONE, HTML_BODY
from .utils import expandPath


# Set up logging.
logger = logging.getLogger(__name__)
logging.basicConfig()


class PathCacheDict(defaultdict):
    """ Cache if file paths referenced more than once in a file exist, so we don't check on disk over and over.
    """
    def __missing__(self, key):
        self[key] = QFile.exists(key)
        return self[key]


class SaveFileError(Exception):
    """ Exception when saving files, where details can be used to provide the earlier traceback for the user in the
    error dialog's details section.
    """
    def __init__(self, message, details=None):
        """ Initialize the exception.
        
        :Parameters:
            message : `str`
                Message
            details : `str` | None
                Optional traceback to accompany this message.
        """
        super(SaveFileError, self).__init__(message)
        self.details = details


class FileParser(QObject):
    """ Base class for RegEx-based file parsing.
    """
    progress = Signal(int)
    status = Signal(str)
    
    # Override as needed.
    fileFormat = FILE_FORMAT_NONE
    lineCharLimit = LINE_CHAR_LIMIT

    # If the file format is binary or not (e.g. USD's crate format).
    binary = False

    # Optional icon to display in the tab bar when this file parser is used.
    icon = QIcon()

    # Group within the RegEx corresponding to the file path only.
    # Useful if you modify compile() but not linkParse().
    RE_FILE_GROUP = 1
    
    def __init__(self, parent=None):
        """ Initialize the parser.
        
        :Parameters:
            parent : `QObject`
                Parent object (main window)
        """
        super(FileParser, self).__init__(parent)

        # List of args to pass to addAction on the Commands menu.
        # Each item in the list represents a new menu item.
        self.plugins = []
    
        self.regex = None
        self._stop = False
        self._html_cache = None
        self._html_dirty = True
        
        # Regex compilation cache - class-level to share across instances
        if not hasattr(FileParser, '_regex_cache'):
            FileParser._regex_cache = {}
            FileParser._regex_cache_max_size = 20
        
        self.cleanup()
        
        self.progress.connect(parent.setLoadingProgress)
        self.status.connect(parent.loadingProgressLabel.setText)
        parent.actionStop.triggered.connect(self.stopTriggered)
        parent.compileLinkRegEx.connect(self.compile)
    
    def acceptsFile(self, fileInfo, link):
        """ Determine if this parser can accept the incoming file.
        Note: Parsers check this in a non-deterministic order. Ensure multiple parsers don't accept the same file.
        
        Override in subclass to filter for files this parser can support.
        
        :Parameters:
            fileInfo : `QFileInfo`
                File info object
            link : `QtCore.QUrl`
                Full URL, potentially with query string
        :Returns:
            It the parser should be able to handle the file
        :Rtype:
            `bool`
        """
        raise NotImplementedError
    
    def cleanup(self):
        """ Reset variables for a new file.
        
        Don't override.
        """
        self.exists = PathCacheDict()
        self._html_cache = None
        self._html_dirty = True
        self.text = []
        self.truncated = False
        self.warning = None
    
    @Slot()
    def compile(self):
        """ Compile regular expression to find links based on the acceptable extensions stored in self.programs.
        
        Override for language-specific RegEx.
        
        NOTE: If this RegEx changes, the syntax highlighting rules may need to as well.
        """
        exts = self.parent().programs.keys()
        pattern_key = '|'.join(sorted(exts))
        
        # Check cache first
        if pattern_key in FileParser._regex_cache:
            self.regex = FileParser._regex_cache[pattern_key]
            logger.debug("Using cached regex for pattern: %s", pattern_key[:50] + "..." if len(pattern_key) > 50 else pattern_key)
            return
        
        # Compile new regex
        self.regex = re.compile(
            r'(?:[\'"@]+)'                    # 1 or more single quote, double quote, or at symbol.
            r'('                              # Group 1: Path. This is the main group we are looking for. Matches based on extension before the pipe, or variable after the pipe.
                r'[^\t\n\r\f\v\'"]*?'         # 0 or more (greedy) non-whitespace characters (regular spaces are ok) and no quotes followed by a period, then 1 of the acceptable file extensions.
                r'\.(?:'+'|'.join(exts)+r')'  # followed by a period, then 1 of the acceptable file extensions
                r'|\${[\w/${}:.-]+}'          # One or more of these characters -- A-Za-z0-9_-/${}:. -- inside the variable curly brackets -- ${}
            r')'                              # end group 1
            r'(?:[\'"@]|\\\")',  # 1 of: single quote, double quote, backslash followed by double quote, or at symbol.
            re.IGNORECASE | re.MULTILINE  # Optimize with useful flags
        )
        
        # Cache the compiled regex
        FileParser._regex_cache[pattern_key] = self.regex
        
        # Implement LRU-style cache eviction
        if len(FileParser._regex_cache) > FileParser._regex_cache_max_size:
            # Remove oldest entry (simple FIFO for now)
            oldest_key = next(iter(FileParser._regex_cache))
            del FileParser._regex_cache[oldest_key]
            logger.debug("Evicted oldest regex from cache")
        
        logger.debug("Compiled and cached new regex for pattern: %s", pattern_key[:50] + "..." if len(pattern_key) > 50 else pattern_key)

    @staticmethod
    def generateTempFile(fileName, tmpDir=None):
        """ For file formats supporting ASCII and binary representations, generate a temporary ASCII file that the user can edit.
        
        :Parameters:
            fileName : `str`
                Binary file path
            tmpDir : `str` | None
                Temp directory to create the new file within
        :Returns:
            Temporary file name
        :Rtype:
            `str`
        """
        raise NotImplementedError

    def parse(self, nativeAbsPath, fileInfo, link):
        """ Parse a file for links, generating a plain text version and HTML version of the file text.
        
        In general, don't override unless you need to add something before parsing really starts, and then just call
        super() for the rest of this method.
        
        :Parameters:
            nativeAbsPath : `str`
                OS-native absolute file path
            fileInfo : `QFileInfo`
                File info object
            link : `QUrl`
                Full file path URL
        """
        self.cleanup()
        
        # Fast path for raw mode - bypass heavy processing
        if self._stop:
            logger.debug("Using fast raw mode parsing for file: %s", nativeAbsPath)
            return self.parseRawMode(nativeAbsPath)
        
        self.status.emit("Reading file (Normal Mode)")
        logger.debug("Using normal parsing mode for file: %s", nativeAbsPath)
        self.text = self.read(nativeAbsPath)
        
        # TODO: Figure out a better way to handle streaming text for large files like Crate geometry.
        # Large chunks of text (e.g. 2.2 billion characters) will cause Qt to segfault when creating a QString.
        length = len(self.text)
        if length > self.parent().preferences['lineLimit']:
            length = self.parent().preferences['lineLimit']
            self.truncated = True
            self.text = self.text[:length]
            self.warning = "Extremely large file! Capping display at {:,d} lines. You can edit this cap in the "\
                           "Advanced tab of Preferences.".format(length)
        self.parent().loadingProgressBar.setMaximum(length)
        
        if self._stop:
            self.status.emit("Parsing text")
            logger.debug("Parsing text.")
        else:
            self.status.emit("Parsing text for links")
            logger.debug("Parsing text for links.")
        
        # Reduce name lookups for speed, since this is one of the slowest parts of the app.
        emit = self.progress.emit
        lineCharLimit = self.lineCharLimit
        finditer = self.regex.finditer
        re_file_group = self.RE_FILE_GROUP
        parseMatch = self.parseMatch

        html = ""
        # Escape HTML characters for proper display.
        # Do this before we add any actual HTML characters.
        lines = [escape(x) for x in self.text]
        
        # Batch progress updates to reduce Qt signal overhead
        progress_batch_size = 1000  # Emit progress every 1000 lines instead of every line
        
        for i, line in enumerate(lines):
            if self._stop:
                # If the user has requested to stop, load the rest of the document
                # without doing the expensive parsing for links.
                html += "".join(lines[i:])
                break
            
            # Batch progress updates for better performance
            if i % progress_batch_size == 0:
                emit(i)
            if len(line) > lineCharLimit:
                html += self.parseLongLine(line)
                continue
            
            # Search for multiple, non-overlapping links on each line.
            offset = 0
            for m in finditer(line):
                # Since we had to escape all potential HTML-related characters before finding links, undo any replaced
                # by escape if part of the linkPath itself. URIs may have & as part of the path for query parameters.
                # We then have to re-escape the path before inserting it into HTML.
                linkPath = unescape(m.group(re_file_group))
                start = m.start(re_file_group)
                end = m.end(re_file_group)
                try:
                    href = parseMatch(m, linkPath, nativeAbsPath, fileInfo)
                except ValueError:
                    # File doesn't exist or path cannot be resolved.
                    # Color it red.
                    href = '<span title="File not found" class="badLink">{}</span>'.format(escape(linkPath))
                # Calculate difference in length between new link and original text so that we know where
                # in the string to start the replacement when we have multiple matches in the same line.
                line = line[:start + offset] + href + line[end + offset:]
                offset += len(href) - end + start
            html += line
        
        logger.debug("Done parsing text for links.")
        if len(html) > CHAR_LIMIT:
            self.truncated = True
            html = html[:CHAR_LIMIT]
            self.warning = "Extremely large file! Capping display at {:,d} characters.".format(CHAR_LIMIT)
        
        # Wrap the final text in a proper HTML document.
        self.html = self.htmlFormat(html)
    
    def htmlFormat(self, text):
        """ Wrap the final text in a proper HTML document.
        
        Override to add additional HTML tags only to the HTML representation of this file.
        
        :Parameters:
            text : `str`
        :Returns:
            HTML text document
        :Rtype:
            `str`
        """
        return HTML_BODY.format(text)
    
    @property
    def html(self):
        """ Lazy HTML generation - only create when actually needed.
        
        :Returns:
            HTML representation of the file
        :Rtype:
            `str`
        """
        if self._html_cache is None or self._html_dirty:
            if self._stop:  # Raw mode
                self._html_cache = self.generateMinimalHtml()
            else:
                # Normal mode - generate full HTML if not already cached
                if not hasattr(self, '_processed_html'):
                    self._processed_html = ""
                self._html_cache = self._processed_html
            self._html_dirty = False
        return self._html_cache
    
    @html.setter
    def html(self, value):
        """ Set the HTML cache directly (for backward compatibility).
        
        :Parameters:
            value : `str`
                HTML content
        """
        self._html_cache = value
        self._html_dirty = False
        if not self._stop:  # In normal mode, also store as processed HTML
            self._processed_html = value
    
    def parseRawMode(self, nativeAbsPath):
        """ Ultra-fast parsing for Raw View mode - bypasses link parsing and minimal HTML generation.
        
        :Parameters:
            nativeAbsPath : `str`
                OS-native absolute file path
        """
        self.status.emit("Reading file (Fast Raw Mode)")
        logger.debug("*** FAST RAW MODE ENGAGED for %s ***", nativeAbsPath)
        self.text = self.read(nativeAbsPath)
        
        # Apply line limit if necessary
        length = len(self.text)
        if hasattr(self.parent(), 'preferences') and length > self.parent().preferences.get('lineLimit', 1000000):
            limit = self.parent().preferences['lineLimit']
            self.truncated = True
            self.text = self.text[:limit]
            self.warning = "Extremely large file! Capping display at {:,d} lines. You can edit this cap in the "\
                          "Advanced tab of Preferences.".format(limit)
        
        # Set progress bar maximum (much faster without per-line updates)
        if hasattr(self.parent(), 'loadingProgressBar'):
            self.parent().loadingProgressBar.setMaximum(length)
        
        # Mark HTML as dirty - will be generated lazily when accessed
        self._html_dirty = True
        
        logger.debug("Raw mode parsing complete")
    
    def generateMinimalHtml(self):
        """ Generate minimal HTML for raw mode - just escaped text in a pre tag.
        
        :Returns:
            Minimal HTML representation
        :Rtype:
            `str`
        """
        # Join all text and escape HTML characters once
        import html
        escaped_text = html.escape(''.join(self.text))
        
        # Simple pre-formatted wrapper - no syntax highlighting, no links
        simple_html = f'<pre style="font-family: monospace; white-space: pre-wrap; margin: 0; padding: 8px;">{escaped_text}</pre>'
        
        return HTML_BODY.format(simple_html)
    
    def parseMatch(self, match, linkPath, nativeAbsPath, fileInfo):
        """ Parse a RegEx match of a patch to another file.
        
        Override for specific language parsing.
        
        :Parameters:
            match
                RegEx match object
            linkPath : `str`
                Displayed file path matched by the RegEx
            nativeAbsPath : `str`
                OS-native absolute file path for the file being parsed
            fileInfo : `QFileInfo`
                File info object for the file being parsed
        :Returns:
            HTML link
        :Rtype:
            `str`
        :Raises ValueError:
            If path does not exist or cannot be resolved.
        """
        # linkPath = `str` displayed file path
        # fullPath = `str` absolute file path
        # Example: <a href="fullPath">linkPath</a>
        if QFileInfo(linkPath).isAbsolute():
            fullPath = QFileInfo(expandPath(linkPath, nativeAbsPath)).absoluteFilePath()
            logger.debug("Parsed link is absolute (%s). Expanded to %s", linkPath, fullPath)
        else:
            # Relative path from the current file to the link.
            fullPath = fileInfo.dir().absoluteFilePath(expandPath(linkPath, nativeAbsPath))
            logger.debug("Parsed link is relative (%s). Expanded to %s", linkPath, fullPath)
        
        # Make the HTML link.
        if self.exists[fullPath]:
            return '<a href="file://{}">{}</a>'.format(fullPath, escape(linkPath))
        elif '*' in linkPath or '<UDIM>' in linkPath or '.#.' in linkPath:
            # Create an orange link for files with wildcards in the path,
            # designating zero or more files may exist.
            return '<a title="Multiple files may exist" class="mayNotExist" href="file://{}">{}</a>'.format(
                fullPath, escape(linkPath))
        return '<a title="File not found" class="badLink" href="file://{}">{}</a>'.format(fullPath, escape(linkPath))
    
    def parseLongLine(self, line):
        """ Process a long line. Link parsing is skipped by default for lines over a certain length.
        
        Override if desired, like truncating the display of a long array.
        
        :Parameters:
            line : `str`
                Line of text
        :Returns:
            Line of text
        :Rtype:
            `str`
        """
        logger.debug("Skipping link parsing for long line")
        return line
    
    def read(self, path):
        """
        :Parameters:
            path : `str`
                OS-native absolute file path
        :Returns:
            List of lines of text of file.
            Can be overridden by subclasses to handle things like crate conversion from binary to ASCII.
        :Rtype:
            [`str`]
        """
        # Use streaming reader for raw mode to improve performance with large files
        if self._stop:
            return self.readStreaming(path)
        
        with open(path) as f:
            return f.readlines()
    
    def readStreaming(self, path, chunk_size=None):
        """Stream file in chunks with content-aware loading strategies.
        
        :Parameters:
            path : `str`
                OS-native absolute file path
            chunk_size : `int` | None
                Size of chunks to read at once (auto-determined if None)
        :Returns:
            List of lines of text, potentially truncated for very large files
        :Rtype:
            [`str`]
        """
        try:
            # Content-aware loading strategy
            strategy = self.getLoadingStrategy(path)
            chunk_size = chunk_size or strategy['chunk_size']
            max_size = strategy['max_size']
            
            logger.debug("Using loading strategy: %s for file %s", strategy['name'], path)
            
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                content_chunks = []
                total_size = 0
                
                while total_size < max_size:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    content_chunks.append(chunk)
                    total_size += len(chunk)
                    
                    # Adaptive chunk size for very large files
                    if strategy['adaptive'] and total_size > 10 * 1024 * 1024:  # After 10MB
                        chunk_size = min(chunk_size * 2, 64 * 1024)  # Double chunk size up to 64KB
                
                # Join chunks and split into lines
                content = ''.join(content_chunks)
                
                if total_size >= max_size:
                    self.truncated = True
                    self.warning = f"Large file truncated at {max_size:,} characters for Raw View performance"
                
                # Split into lines, preserving line endings
                return content.splitlines(keepends=True)
                
        except Exception as e:
            logger.error(f"Error reading file in streaming mode: {e}")
            return [f"Error reading file: {e}\n"]
    
    def getLoadingStrategy(self, path):
        """Determine optimal loading strategy based on file characteristics.
        
        :Parameters:
            path : `str`
                File path to analyze
        :Returns:
            Strategy configuration dictionary
        :Rtype:
            `dict`
        """
        from Qt.QtCore import QFileInfo
        
        file_info = QFileInfo(path)
        file_size = file_info.size()
        extension = file_info.suffix().lower()
        
        # Get user preferences
        prefs = getattr(self.parent(), 'preferences', {})
        base_max_size = prefs.get('rawModeMaxSize', 50 * 1024 * 1024)
        
        # Content-aware strategies
        if file_size > 100 * 1024 * 1024:  # >100MB
            return {
                'name': 'streaming_huge',
                'chunk_size': 32 * 1024,  # 32KB chunks
                'max_size': min(base_max_size, 20 * 1024 * 1024),  # Limit to 20MB for huge files
                'adaptive': True
            }
        elif file_size > 20 * 1024 * 1024:  # >20MB
            return {
                'name': 'streaming_large',
                'chunk_size': 16 * 1024,  # 16KB chunks
                'max_size': base_max_size,
                'adaptive': True
            }
        elif extension in ('usdc', 'usd') and file_size > 5 * 1024 * 1024:  # USD files >5MB
            return {
                'name': 'streaming_usd_optimized',
                'chunk_size': 12 * 1024,  # 12KB chunks - good for USD structure
                'max_size': base_max_size,
                'adaptive': False
            }
        else:  # Small files
            return {
                'name': 'streaming_default',
                'chunk_size': 8 * 1024,  # 8KB chunks
                'max_size': base_max_size,
                'adaptive': False
            }
    
    def stop(self, stop=True):
        """ Request to stop parsing the active file for links.
        
        Don't override.
        
        :Parameters:
            stop : `bool`
                To stop or not
        """
        self._stop = stop
        if stop:
            logger.debug("Parser stop flag set to True - will use raw mode")

    @Slot(bool)
    def stopTriggered(self, checked=False):
        """ Request to stop parsing the active file for links.
        
        Don't override.
        
        :Parameters:
            checked : `bool`
                For signal only
        """
        self.stop()

    def write(self, qFile, filePath, tab, tmpDir):
        """ Write out a plain text file.

        :Parameters:
            qFile : `QtCore.QFile`
                Object representing the file to write to
            filePath : `str`
                File path to write to
            tab : `str`
                Tab being written
            tmpDir : `str`
                Temporary directory, if needed for any write operations.
        :Raises SaveFileError:
            If the file write fails.
        """
        if not qFile.open(QIODevice.WriteOnly | QIODevice.Text):
            raise SaveFileError("The file could not be opened for saving!")

        try:
            out = QTextStream(qFile)
            _ = out << tab.textEditor.toPlainText()
        except Exception:
            raise SaveFileError("The file could not be saved.", traceback.format_exc())
        finally:
            qFile.close()

        tab.parser = self
        tab.fileFormat = self.fileFormat


class AbstractExtParser(FileParser):
    """ Determines which files are supported based on extension.
    Override exts in a subclass to add extensions.
    """
    # Tuple of `str` file extensions (without the leading .) that this parser can support. Example: ("usda",)
    exts = ()

    def acceptsFile(self, fileInfo, link):
        """ Accept files with the proper extension.
        
        :Parameters:
            fileInfo : `QFileInfo`
                File info object
            link : `QtCore.QUrl`
                Full URL, potentially with query string
        """
        return fileInfo.suffix() in self.exts
