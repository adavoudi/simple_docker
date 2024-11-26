# utils.py

def decode_image(image):
    # Parse image_name and tag
    if ':' in image:
        image_name, tag = image.split(':', 1)
    else:
        image_name = image
        tag = 'latest'
    return image_name, tag
