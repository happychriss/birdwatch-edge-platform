import tensorflow as tf
from PIL import Image
from tensorflow.keras.layers import Input, GlobalAveragePooling2D, Dense
from tensorflow.keras.applications.mobilenet_v2 import MobileNetV2
from tensorflow.keras.models import Model
from tensorflow.keras.preprocessing.image import ImageDataGenerator

dataset_path = './datasets/pigeons_roboflow' #local
# dataset_path = ./pigeons_roboflow/training' # colab
pic_size = 224


import tensorflow as tf
from PIL import Image
from tensorflow.keras.layers import Input, GlobalAveragePooling2D, Dense
from tensorflow.keras.applications.mobilenet_v2 import MobileNetV2
from tensorflow.keras.models import Model
from tensorflow.keras.preprocessing.image import ImageDataGenerator
from tensorflow.keras.applications import EfficientNetB0
from tensorflow.keras.layers import GlobalAveragePooling2D, Dense
from tensorflow.keras.models import Model
from tensorflow.keras.callbacks import EarlyStopping, ReduceLROnPlateau


# dataset https://universe.roboflow.com/wiings/pigeon-mg46t/dataset/1#

def build_model(num_classes):
    base_model = EfficientNetB0(input_shape=(224, 224, 3), include_top=False, weights='imagenet')
    x = base_model.output
    x = GlobalAveragePooling2D()(x)
    x = Dense(128, activation='relu')(x)
    predictions = Dense(num_classes, activation='sigmoid' if num_classes == 1 else 'softmax')(x)
    model = Model(inputs=base_model.input, outputs=predictions)
    return model


num_classes = 2  # Bird and non-bird
model = build_model(num_classes)
model.summary()

model.compile(optimizer='adam', loss='categorical_crossentropy', metrics=['accuracy'])


# Setup ImageDataGenerators
train_datagen = ImageDataGenerator(
    rescale=1./255,
    rotation_range=20,
    width_shift_range=0.2,
    height_shift_range=0.2,
    shear_range=0.2,
    zoom_range=0.2,
    horizontal_flip=True,
    fill_mode='nearest',
    validation_split=0.2  # Use 20% of the data for validation
)

val_datagen = ImageDataGenerator(
    rescale=1./255,
    validation_split=0.2  # Same validation split for consistency
)

# Setup training and validation generators
train_generator = train_datagen.flow_from_directory(
    dataset_path+'/training',
    target_size=(224, 224),
    batch_size=32,
    class_mode='categorical',
    subset='training',  # Set as 'training' data
    shuffle=True
)

validation_generator = val_datagen.flow_from_directory(
    dataset_path+'/training',
    target_size=(224, 224),
    batch_size=32,
    class_mode='categorical',
    subset='validation',  # Set as 'validation' data
    shuffle=False
)

test_datagen = ImageDataGenerator(rescale=1.0/255)

test_generator = test_datagen.flow_from_directory(
    dataset_path+'/test',
    target_size=(224, 224),
    batch_size=32,
    class_mode='categorical'
)

# Callbacks
early_stopping = EarlyStopping(monitor='val_loss', patience=10, verbose=1, mode='min', restore_best_weights=True)
reduce_lr = ReduceLROnPlateau(monitor='val_loss', factor=0.2, patience=5, verbose=1, mode='min')

# Train model
history = model.fit(
    train_generator,
    steps_per_epoch=len(train_generator),
    epochs=100,  # start with 100, but may stop early
    validation_data=validation_generator,
    validation_steps=len(validation_generator),
    callbacks=[early_stopping, reduce_lr]
)


test_loss, test_acc = model.evaluate(test_generator, verbose=2)
print("\nTest accuracy:", test_acc)