"""Decode complete section objects without exposing partial JSON strings."""
import json
import re

def completed_sections(buffer):
    match=re.match(r'\s*\{\s*"sections"\s*:\s*\[',buffer)
    if match is None:return []
    position=match.end();result=[];decoder=json.JSONDecoder()
    while position<len(buffer):
        while position<len(buffer) and buffer[position].isspace():position+=1
        if position>=len(buffer) or buffer[position]==']':break
        if result:
            if buffer[position]!=',':break
            position+=1
            while position<len(buffer) and buffer[position].isspace():position+=1
        try:value,end=decoder.raw_decode(buffer,position)
        except json.JSONDecodeError:break
        result.append(value);position=end
    return result
